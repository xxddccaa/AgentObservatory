"""Retention: plan and execute cleanup of local agent data.

The Storage page never deletes anything itself. It asks for a *plan*: a
read-only inventory of files older than the requested window, grouped by agent
and category.  The plan is sealed into an HMAC token and only the Supervisor
turns it into deletions, so every destructive step is (a) derived from a
server-side allow-list of roots, (b) above the configured minimum retention
window, and (c) recorded in ``audit_logs`` and ``retention_runs``.

Safety invariants enforced here, not in the browser:

* the client can only send a *selection* (agents / categories / keep-days);
  paths are always re-derived from the catalog below;
* nothing modified inside ``AGENT_OBS_RETENTION_MIN_HOURS`` is ever planned;
* a path must resolve inside its catalog root (symlinks never escape it);
* roots listed in ``AGENT_OBS_RETENTION_PROTECTED_ROOTS`` are skipped;
* execution re-plans from scratch and refuses when the live plan grew past
  the previewed size.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from collections import deque
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from agent_observatory.config import settings
from agent_observatory.db.models import AgentSession, LocalDataSource, RetentionRun
from agent_observatory.services import audit

logger = logging.getLogger(__name__)

#: Risk tiers. ``regenerable`` data is rebuilt by the agent itself, ``history``
#: is user-visible conversation history, ``runtime`` is state an agent needs to
#: keep working (installed versions, resume state).
REGENERABLE = "regenerable"
HISTORY = "history"
RUNTIME = "runtime"

RISK_ORDER = {REGENERABLE: 0, HISTORY: 1, RUNTIME: 2}

RUN_PENDING = "pending"
RUN_RUNNING = "running"
RUN_APPLIED = "applied"
RUN_FAILED = "failed"
RUN_REFUSED = "refused"

#: Hard ceiling used while walking a tree. A single ``os.scandir`` pass per
#: directory keeps planning bounded and never loads file contents.
MAX_WALK_ENTRIES_PER_ROOT = 20_000

#: Upper bound on rows pulled per catalog entry while matching the index.
MAX_INDEX_ROWS = 100_000


class RetentionError(RuntimeError):
    """Raised when a selection or plan violates a safety invariant."""


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One selectable pickup location on this machine."""

    id: str
    agent: str
    category: str
    label: str
    root: str
    unit: str  # "file" | "dir"
    patterns: tuple[str, ...]
    risk: str
    description: str = ""

    @property
    def root_path(self) -> Path:
        return Path(self.root)


@dataclass(frozen=True, slots=True)
class Selection:
    """What the operator asked to clean, exactly as sent by the browser."""

    keep_days: int = 7
    agents: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    entry_ids: tuple[str, ...] = ()
    include_raw: bool = True
    include_index: bool = True

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> Selection:
        payload = payload or {}

        def _tuple(value: Any, label: str) -> tuple[str, ...]:
            if value in (None, ""):
                return ()
            if isinstance(value, str):
                items = [value]
            elif isinstance(value, (list, tuple)):
                items = [str(item) for item in value]
            else:
                raise RetentionError(f"{label} must be a list of strings")
            cleaned = tuple(dict.fromkeys(item.strip() for item in items if item.strip()))
            if any(len(item) > 128 or "/" in item or ":" in item for item in cleaned):
                raise RetentionError(f"{label} contains an invalid value")
            return cleaned

        raw_days = payload.get("keep_days", 7)
        try:
            keep_days = int(raw_days)
        except (TypeError, ValueError) as exc:
            raise RetentionError("keep_days must be an integer") from exc
        if keep_days < 1 or keep_days > 3650:
            raise RetentionError("keep_days must be between 1 and 3650")
        return cls(
            keep_days=keep_days,
            agents=_tuple(payload.get("agents"), "agents"),
            categories=_tuple(payload.get("categories"), "categories"),
            entry_ids=_tuple(payload.get("entry_ids"), "entry_ids"),
            include_raw=bool(payload.get("include_raw", True)),
            include_index=bool(payload.get("include_index", True)),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "keep_days": self.keep_days,
            "agents": list(self.agents),
            "categories": list(self.categories),
            "entry_ids": list(self.entry_ids),
            "include_raw": self.include_raw,
            "include_index": self.include_index,
        }


@dataclass(slots=True)
class PlannedItem:
    path: str
    entry_id: str
    agent: str
    category: str
    risk: str
    kind: str
    bytes: int
    mtime: float


@dataclass(slots=True)
class Plan:
    selection: Selection
    generated_at: datetime
    floor_at: datetime
    items: list[PlannedItem] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    entries: list[dict[str, Any]] = field(default_factory=list)
    index: dict[str, Any] = field(default_factory=dict)
    truncated: bool = False

    @property
    def bytes_total(self) -> int:
        return sum(item.bytes for item in self.items)

    def summary(self) -> dict[str, Any]:
        by_agent: dict[str, dict[str, Any]] = {}
        by_category: dict[str, dict[str, Any]] = {}
        by_risk: dict[str, dict[str, Any]] = {}
        for item in self.items:
            for bucket, key in (
                (by_agent, item.agent),
                (by_category, item.category),
                (by_risk, item.risk),
            ):
                slot = bucket.setdefault(key, {"bytes": 0, "files": 0})
                slot["bytes"] += item.bytes
                slot["files"] += 1
        return {
            "floor_at": self.floor_at.isoformat(),
            "generated_at": self.generated_at.isoformat(),
            "keep_days": self.selection.keep_days,
            "totals": {
                "bytes": self.bytes_total,
                "files": len(self.items),
                "entries": len(
                    {item.entry_id for item in self.items}
                ),
                "sessions": int(self.index.get("sessions", 0)),
                "sources": int(self.index.get("sources", 0)),
            },
            "by_agent": by_agent,
            "by_category": by_category,
            "by_risk": by_risk,
            "index": self.index,
            "entries": self.entries,
            "skipped": self.skipped,
            "truncated": self.truncated,
        }


# --------------------------------------------------------------------------
# catalog
# --------------------------------------------------------------------------
def catalog(*, config: Any | None = None) -> list[CatalogEntry]:
    """Return the pick-up catalog for this machine (paths already expanded)."""
    config = config or settings()
    codex_home = Path(
        getattr(config, "codex_home", str(Path("~/.codex").expanduser()))
    ).expanduser()
    stepcode_home = Path(
        getattr(config, "stepcode_root", "/root/.stepcode/codex")
    ).expanduser().parent
    stepcli_home = Path(getattr(config, "stepcli_home", "/root/.step-cli")).expanduser()
    claude_home = Path(
        getattr(config, "claude_projects_root", "/root/.claude/projects")
    ).expanduser().parent
    dsh_home = Path("~/.dsh").expanduser()
    opencode_home = Path("~/.local/share/opencode").expanduser()
    observatory_home = Path(getattr(config, "home", "/root/.agent-observatory")).expanduser()

    raw: list[CatalogEntry] = [
        CatalogEntry(
            "codex.sessions",
            "codex",
            "sessions",
            "Codex rollout sessions",
            str(codex_home / "sessions"),
            "file",
            ("**/*.jsonl", "**/*.jsonl.zst"),
            HISTORY,
            "Rollout transcripts. Deleting them removes the sessions from Codex and this observatory.",
        ),
        CatalogEntry(
            "codex.logs",
            "codex",
            "logs",
            "Codex service logs",
            str(codex_home),
            "file",
            ("logs_2.sqlite", "logs_2.sqlite-wal", "logs_2.sqlite-shm"),
            REGENERABLE,
            "Diagnostic log database; Codex recreates it.",
        ),
        CatalogEntry(
            "codex.state",
            "codex",
            "state",
            "Codex state + thread history",
            str(codex_home),
            "file",
            (
                "thread_history_1.sqlite",
                "thread_history_1.sqlite-wal",
                "thread_history_1.sqlite-shm",
                "state_5.sqlite",
                "state_5.sqlite-wal",
                "state_5.sqlite-shm",
                "goals_1.sqlite",
                "goals_1.sqlite-wal",
                "goals_1.sqlite-shm",
                "history.jsonl",
            ),
            RUNTIME,
            "Thread history and CLI state. Removing it makes Codex forget past threads.",
        ),
        CatalogEntry(
            "stepcode.sessions",
            "stepcode",
            "sessions",
            "StepCode sessions",
            str(stepcode_home / "sessions"),
            "dir",
            ("*",),
            HISTORY,
            "StepCode session transcripts.",
        ),
        CatalogEntry(
            "stepcode.codex_logs",
            "stepcode",
            "logs",
            "StepCode codex logs",
            str(stepcode_home / "codex"),
            "file",
            ("logs_2.sqlite", "logs_2.sqlite-wal", "logs_2.sqlite-shm"),
            REGENERABLE,
            "StepCode's embedded codex log database.",
        ),
        CatalogEntry(
            "stepcode.logs",
            "stepcode",
            "logs",
            "StepCode logs",
            str(stepcode_home / "logs"),
            "file",
            ("**/*.log", "**/*.jsonl", "**/*.jsonl.*"),
            REGENERABLE,
            "StepCode run logs.",
        ),
        CatalogEntry(
            "stepcode.snapshots",
            "stepcode",
            "snapshots",
            "UX-plan snapshots",
            str(stepcode_home / "ux-plan" / "snapshots"),
            "dir",
            ("*",),
            RUNTIME,
            "Resume snapshots for ux-plan runs.",
        ),
        CatalogEntry(
            "stepcode.versions",
            "stepcode",
            "runtime",
            "Installed StepCode versions",
            str(stepcode_home / "versions"),
            "dir",
            ("*",),
            RUNTIME,
            "Downloaded CLI versions. Delete only versions you no longer pin.",
        ),
        CatalogEntry(
            "stepcli.sessions",
            "stepcli",
            "sessions",
            "Step CLI sessions",
            str(stepcli_home / "sessions"),
            "dir",
            ("*",),
            HISTORY,
            "Step CLI session directories.",
        ),
        CatalogEntry(
            "stepcli.logs",
            "stepcli",
            "logs",
            "Step CLI logs",
            str(stepcli_home / "logs"),
            "file",
            ("**/*.log", "**/*.jsonl", "**/*.jsonl.*"),
            REGENERABLE,
            "Step CLI run logs.",
        ),
        CatalogEntry(
            "stepcli.telemetry",
            "stepcli",
            "logs",
            "Step CLI telemetry",
            str(stepcli_home / "telemetry"),
            "file",
            ("**/*",),
            REGENERABLE,
            "Local telemetry buffers.",
        ),
        CatalogEntry(
            "claude.sessions",
            "claude",
            "sessions",
            "Claude transcripts",
            str(claude_home / "projects"),
            "file",
            ("**/*.jsonl",),
            HISTORY,
            "Claude Code transcripts, including subagent sidecars.",
        ),
        CatalogEntry(
            "claude.file_history",
            "claude",
            "caches",
            "Claude file-edit history",
            str(claude_home / "file-history"),
            "dir",
            ("*",),
            REGENERABLE,
            "Undo history for file edits.",
        ),
        CatalogEntry(
            "claude.paste_cache",
            "claude",
            "caches",
            "Claude paste cache",
            str(claude_home / "paste-cache"),
            "file",
            ("**/*",),
            REGENERABLE,
            "Cached clipboard payloads.",
        ),
        CatalogEntry(
            "dsh.sessions",
            "dsh",
            "sessions",
            "DSH sessions",
            str(dsh_home / "sessions"),
            "dir",
            ("*",),
            HISTORY,
            "DSH session directories.",
        ),
        CatalogEntry(
            "opencode.data",
            "opencode",
            "sessions",
            "OpenCode data",
            str(opencode_home),
            "file",
            ("**/*",),
            HISTORY,
            "OpenCode database, logs and repo cache.",
        ),
        CatalogEntry(
            "observatory.logs",
            "observatory",
            "logs",
            "Observatory logs",
            str(observatory_home / "logs"),
            "file",
            ("*.jsonl", "*.jsonl.*"),
            REGENERABLE,
            "Supervisor, API and worker logs.",
        ),
    ]
    return [entry for entry in raw]


def catalog_by_id(*, config: Any | None = None) -> dict[str, CatalogEntry]:
    return {entry.id: entry for entry in catalog(config=config)}


def _selection_entries(
    selection: Selection, entries: Sequence[CatalogEntry]
) -> list[CatalogEntry]:
    selected: list[CatalogEntry] = []
    for entry in entries:
        if selection.entry_ids and entry.id not in selection.entry_ids:
            continue
        if selection.agents and entry.agent not in selection.agents:
            continue
        if selection.categories and entry.category not in selection.categories:
            continue
        selected.append(entry)
    if not selected:
        raise RetentionError("no data categories matched this selection")
    known = {entry.agent for entry in entries}
    unknown = [agent for agent in selection.agents if agent not in known]
    if unknown:
        raise RetentionError(f"unknown agent(s): {', '.join(unknown)}")
    known_categories = {entry.category for entry in entries}
    unknown_categories = [c for c in selection.categories if c not in known_categories]
    if unknown_categories:
        raise RetentionError(
            f"unknown category(ies): {', '.join(unknown_categories)}"
        )
    return selected


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------
def floor_for(selection: Selection, *, now: datetime | None = None) -> datetime:
    """Deletion frontier: items **older** than this may be removed.

    ``keep_days`` moves the frontier back; ``AGENT_OBS_RETENTION_MIN_HOURS``
    caps how recent the frontier may ever be, so nothing touched inside the
    minimum window is deletable no matter what the caller asks for.
    """
    config = settings()
    now = now or datetime.now(UTC)
    requested = now - timedelta(days=selection.keep_days)
    hard_floor = now - timedelta(hours=int(config.retention_min_hours))
    return min(requested, hard_floor)


def _protected(path: Path) -> bool:
    config = settings()
    for raw in config.retention_protected_roots:
        try:
            protected_root = Path(raw)
        except OSError:  # pragma: no cover - defensive
            continue
        if path == protected_root or protected_root in path.parents:
            return True
    return False


def _within_root(path: Path, root: Path) -> bool:
    resolved_root = root.resolve()
    resolved = path.resolve()
    return resolved == resolved_root or resolved_root in resolved.parents


def _subtree_stats(
    path: Path, *, max_entries: int = MAX_WALK_ENTRIES_PER_ROOT
) -> tuple[int, int, float, bool, bool]:
    """Return (bytes, files, max_mtime, truncated, symlink_escape) for a tree."""
    total = 0
    files = 0
    newest = 0.0
    truncated = False
    visited = 0
    queue: deque[Path] = deque([path])
    while queue:
        current = queue.popleft()
        try:
            with os.scandir(current) as scanner:
                for entry in scanner:
                    if visited >= max_entries:
                        truncated = True
                        return total, files, newest, truncated, False
                    visited += 1
                    if entry.is_symlink():
                        continue
                    try:
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if not _within_root(Path(entry.path), path):
                            return total, files, newest, truncated, True
                        queue.append(Path(entry.path))
                    else:
                        total += stat.st_size
                        files += 1
                        newest = max(newest, stat.st_mtime)
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
            continue
    return total, files, newest, truncated, False


def _iter_pattern_items(root: Path, patterns: Iterable[str]) -> Iterator[Path]:
    for pattern in patterns:
        for candidate in root.glob(pattern):
            if candidate.is_symlink():
                continue
            if candidate.is_file():
                yield candidate


def plan(
    selection: Selection,
    *,
    now: datetime | None = None,
    entries: Sequence[CatalogEntry] | None = None,
    config: Any | None = None,
) -> Plan:
    """Inventory everything the selection would delete. Read-only."""
    config = config or settings()
    now = now or datetime.now(UTC)
    floor = floor_for(selection, now=now)
    floor_ts = floor.timestamp()
    entries = list(entries if entries is not None else catalog(config=config))
    selected = _selection_entries(selection, entries)
    max_entries = int(config.retention_max_entries)

    result = Plan(selection=selection, generated_at=now, floor_at=floor)
    skipped: dict[tuple[str, str], dict[str, Any]] = {}
    visited = 0

    def _skip(entry: CatalogEntry, reason: str, *, count: int = 0, size: int = 0) -> None:
        slot = skipped.setdefault(
            (entry.id, reason),
            {
                "entry_id": entry.id,
                "agent": entry.agent,
                "reason": reason,
                "files": 0,
                "bytes": 0,
            },
        )
        slot["files"] += count
        slot["bytes"] += size

    for entry in selected:
        root = entry.root_path
        entry_bytes = 0
        entry_files = 0
        if not root.exists():
            _skip(entry, "missing")
            continue
        if _protected(root):
            _skip(entry, "protected")
            result.entries.append(_entry_summary(entry, 0, 0, status="protected"))
            continue
        if entry.unit == "dir":
            for child in sorted(root.glob("*")):
                if visited >= max_entries:
                    result.truncated = True
                    break
                visited += 1
                if child.is_symlink() or not child.is_dir():
                    continue
                size, files, newest, truncated, escaped = _subtree_stats(
                    child, max_entries=max(1000, max_entries - visited)
                )
                if escaped or truncated:
                    if truncated:
                        result.truncated = True
                    _skip(entry, "unbounded", count=files, size=size)
                    continue
                if newest > floor_ts:
                    _skip(entry, "in_use", count=files, size=size)
                    continue
                result.items.append(
                    PlannedItem(
                        path=str(child),
                        entry_id=entry.id,
                        agent=entry.agent,
                        category=entry.category,
                        risk=entry.risk,
                        kind="dir",
                        bytes=size,
                        mtime=newest,
                    )
                )
                entry_bytes += size
                entry_files += files
        else:
            for candidate in _iter_pattern_items(root, entry.patterns):
                if visited >= max_entries:
                    result.truncated = True
                    break
                visited += 1
                if _protected(candidate):
                    _skip(entry, "protected", count=1)
                    continue
                try:
                    stat = candidate.stat()
                except OSError:
                    continue
                if stat.st_mtime > floor_ts:
                    _skip(entry, "in_use", count=1, size=stat.st_size)
                    continue
                result.items.append(
                    PlannedItem(
                        path=str(candidate),
                        entry_id=entry.id,
                        agent=entry.agent,
                        category=entry.category,
                        risk=entry.risk,
                        kind="file",
                        bytes=stat.st_size,
                        mtime=stat.st_mtime,
                    )
                )
                entry_bytes += stat.st_size
                entry_files += 1
        result.entries.append(
            _entry_summary(entry, len([i for i in result.items if i.entry_id == entry.id]), entry_bytes)
        )
    result.skipped = list(skipped.values())
    # Entries aggregate must reflect the final item set.
    for summary in result.entries:
        matching = [item for item in result.items if item.entry_id == summary["id"]]
        summary["files"] = len(matching)
        summary["bytes"] = sum(item.bytes for item in matching)
    if result.truncated:
        result.skipped.append(
            {
                "entry_id": "*",
                "agent": "*",
                "reason": "truncated",
                "files": 0,
                "bytes": 0,
            }
        )
    return result


def _entry_summary(
    entry: CatalogEntry, files: int, size: int, *, status: str = "planned"
) -> dict[str, Any]:
    return {
        "id": entry.id,
        "agent": entry.agent,
        "category": entry.category,
        "label": entry.label,
        "root": entry.root,
        "risk": entry.risk,
        "unit": entry.unit,
        "description": entry.description,
        "status": status,
        "files": files,
        "bytes": size,
    }


# --------------------------------------------------------------------------
# plan token
# --------------------------------------------------------------------------
def _secret() -> bytes:
    return settings().api_token_secret.encode()


def sign_plan(selection: Selection, summary: dict[str, Any], *, actor_id: str) -> str:
    payload = {
        "v": 1,
        "actor": actor_id,
        "selection": selection.to_payload(),
        "bytes": summary["totals"]["bytes"],
        "files": summary["totals"]["files"],
        "floor_at": summary["floor_at"],
        "issued_at": datetime.now(UTC).isoformat(),
        "expires_at": (
            datetime.now(UTC) + timedelta(seconds=int(settings().retention_plan_ttl))
        ).isoformat(),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(_secret(), raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=") + "." + base64.urlsafe_b64encode(
        signature
    ).decode().rstrip("=")


def verify_plan(token: str) -> dict[str, Any]:
    try:
        encoded, signature = token.split(".", 1)
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        expected = hmac.new(_secret(), raw, hashlib.sha256).digest()
        provided = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    except (ValueError, TypeError) as exc:
        raise RetentionError("plan token is malformed") from exc
    if not hmac.compare_digest(expected, provided):
        raise RetentionError("plan token failed verification")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise RetentionError("plan token is malformed") from exc
    expires = payload.get("expires_at")
    if not isinstance(expires, str):
        raise RetentionError("plan token is malformed")
    try:
        expiry = datetime.fromisoformat(expires)
    except ValueError as exc:
        raise RetentionError("plan token is malformed") from exc
    if datetime.now(UTC) > expiry:
        raise RetentionError("plan expired; preview again before deleting")
    return payload


# --------------------------------------------------------------------------
# index accounting
# --------------------------------------------------------------------------
def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _coarse_clauses(column: Any, root: str, agents: Iterable[str]) -> list[Any]:
    """Index-friendly prefilter: any row whose path sits under ``root``.

    Local sources store an optional ``<agent>:`` prefix in the path, so the
    prefilter covers every agent prefix in the catalog. Precision comes from the
    in-process matcher below, which only accepts the planned paths.
    """
    base = _escape_like(root.rstrip("/"))
    values = {base, f"{base}/%"}
    for agent in agents:
        prefix = _escape_like(agent)
        values.add(f"{prefix}:{base}")
        values.add(f"{prefix}:{base}/%")
    return [column.like(value, escape="\\") for value in sorted(values)]


def _normalize_source_path(value: str | None) -> str | None:
    """Drop the ``claude:`` / ``stepcli:`` style prefix from an indexed path."""
    if not value:
        return None
    if value.startswith("/"):
        return value
    head, separator, rest = value.partition(":")
    if separator and rest.startswith("/") and head.isidentifier():
        return rest
    return value


def _make_path_matcher(targets: Iterable[str]) -> Any:
    """Return ``f(path) -> bool``: the path is a target or a descendant of one."""
    normalized = {target.rstrip("/") for target in targets if target}

    def matches(value: str) -> bool:
        if value in normalized:
            return True
        current = value
        while "/" in current:
            current = current.rsplit("/", 1)[0]
            if current in normalized:
                return True
        return False

    return matches


def _session_floor_filter(floor: datetime) -> Any:
    stamp = func.coalesce(
        AgentSession.last_activity_at,
        AgentSession.end_time,
        AgentSession.start_time,
        AgentSession.created_at,
    )
    return stamp < floor


def _planned_paths(plan_obj: Plan) -> dict[str, set[str]]:
    grouped: dict[str, set[str]] = {}
    for item in plan_obj.items:
        grouped.setdefault(item.entry_id, set()).add(item.path.rstrip("/"))
    return grouped


async def _session_ids_for_plan(db: AsyncSession, plan_obj: Plan) -> list[str]:
    """Session rows that describe a planned path and are older than the floor."""
    entries = catalog_by_id()
    agents = sorted({entry.agent for entry in entries.values()})
    grouped = _planned_paths(plan_obj)
    if not grouped:
        return []
    matcher = _make_path_matcher(
        {path for paths in grouped.values() for path in paths}
    )
    found: dict[str, None] = {}
    for entry_id in grouped:
        entry = entries.get(entry_id)
        if entry is None:
            continue
        clauses = or_(
            *_coarse_clauses(AgentSession.rollout_real_path, entry.root, agents),
            *_coarse_clauses(AgentSession.source_path, entry.root, agents),
        )
        rows = await db.execute(
            select(
                AgentSession.id,
                AgentSession.rollout_real_path,
                AgentSession.source_path,
            )
            .where(clauses, _session_floor_filter(plan_obj.floor_at))
            .limit(MAX_INDEX_ROWS)
        )
        for row in rows:
            for candidate in (
                _normalize_source_path(row.rollout_real_path),
                _normalize_source_path(row.source_path),
            ):
                if candidate and matcher(candidate):
                    found.setdefault(row.id, None)
                    break
    return list(found)


async def _source_ids_for_plan(db: AsyncSession, plan_obj: Plan) -> list[str]:
    """``local_data_sources`` rows whose file or database is being deleted."""
    entries = catalog_by_id()
    agents = sorted({entry.agent for entry in entries.values()})
    grouped = _planned_paths(plan_obj)
    if not grouped:
        return []
    matcher = _make_path_matcher(
        {path for paths in grouped.values() for path in paths}
    )
    found: dict[str, None] = {}
    for entry_id in grouped:
        entry = entries.get(entry_id)
        if entry is None:
            continue
        rows = await db.execute(
            select(LocalDataSource.id, LocalDataSource.source_path)
            .where(
                or_(*_coarse_clauses(LocalDataSource.source_path, entry.root, agents))
            )
            .limit(MAX_INDEX_ROWS)
        )
        for row in rows:
            candidate = _normalize_source_path(row.source_path)
            if candidate and matcher(candidate):
                found.setdefault(row.id, None)
    return list(found)


async def _index_counts(
    db: AsyncSession, plan_obj: Plan, *, entries: Sequence[CatalogEntry]
) -> dict[str, Any]:
    if not plan_obj.items:
        return {"sessions": 0, "sources": 0, "sessions_by_entry": {}}
    session_rows = await _session_ids_for_plan(db, plan_obj)
    sources = await _source_ids_for_plan(db, plan_obj)
    sessions_by_entry: dict[str, int] = {}
    if session_rows:
        matchers = {
            entry_id: _make_path_matcher(paths)
            for entry_id, paths in _planned_paths(plan_obj).items()
        }
        rows = await db.execute(
            select(
                AgentSession.id, AgentSession.rollout_real_path, AgentSession.source_path
            ).where(AgentSession.id.in_(session_rows))
        )
        for row in rows:
            for entry_id, matcher in matchers.items():
                values = (
                    _normalize_source_path(row.rollout_real_path),
                    _normalize_source_path(row.source_path),
                )
                if any(value and matcher(value) for value in values):
                    sessions_by_entry[entry_id] = sessions_by_entry.get(entry_id, 0) + 1
                    break
    return {
        "sessions": len(session_rows),
        "sources": len(sources),
        "sessions_by_entry": sessions_by_entry,
    }


async def enrich_plan(db: AsyncSession, plan_obj: Plan) -> Plan:
    """Attach index (database) accounting to a filesystem plan."""
    if plan_obj.selection.include_index:
        plan_obj.index = await _index_counts(db, plan_obj, entries=catalog())
    else:
        plan_obj.index = {"sessions": 0, "sources": 0, "sessions_by_entry": {}}
    return plan_obj


# --------------------------------------------------------------------------
# overview
# --------------------------------------------------------------------------
# The cache keeps the monotonic timestamp, a generation stamp and the payload.
# The generation changes on every retention run, so a purge applied by the
# Supervisor (a different process) invalidates this process' cache too.
_overview_cache: tuple[float, str, dict[str, Any]] | None = None


async def overview_generation(db: AsyncSession) -> str:
    """Cheap stamp that changes whenever a retention run progresses.

    ``retention_runs`` is tiny, so one aggregate per request is far cheaper
    than rescanning every catalogued directory; it is what makes a plain page
    reload show post-purge numbers instead of a stale cached scan.
    """
    row = (
        await db.execute(
            text(
                """
                SELECT count(*) AS runs,
                       coalesce(max(created_at), to_timestamp(0)) AS latest_created,
                       coalesce(max(finished_at), to_timestamp(0)) AS latest_finished,
                       count(*) FILTER (
                           WHERE status IN ('pending', 'running', 'failed', 'refused')
                       ) AS unfinished
                FROM retention_runs
                """
            )
        )
    ).first()
    return "|".join(str(value) for value in tuple(row or (0, 0, 0, 0)))


async def overview(db: AsyncSession, *, force: bool = False) -> dict[str, Any]:
    """Per-entry disk usage, cached for ``AGENT_OBS_RETENTION_OVERVIEW_TTL``."""
    global _overview_cache
    config = settings()
    now = time.monotonic()
    generation = await overview_generation(db)
    if (
        not force
        and _overview_cache is not None
        and _overview_cache[1] == generation
        and now - _overview_cache[0] < float(config.retention_overview_ttl)
    ):
        cached = dict(_overview_cache[2])
        cached["cached"] = True
        return cached

    entries = catalog(config=config)
    scans = await _run_in_thread(_scan_entries, entries)
    index = await _index_overview(db)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "cached": False,
        "enabled": bool(config.retention_enabled),
        "min_hours": int(config.retention_min_hours),
        "large_run_bytes": int(config.retention_large_run_bytes),
        "protected_roots": list(config.retention_protected_roots),
        "entries": scans,
        "index": index,
    }
    _overview_cache = (now, await overview_generation(db), payload)
    return dict(payload)


async def _run_in_thread(func: Any, *args: Any) -> Any:
    import asyncio

    return await asyncio.to_thread(func, *args)


def _scan_entries(entries: Sequence[CatalogEntry]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for entry in entries:
        root = entry.root_path
        summary = _entry_summary(entry, 0, 0, status="ok")
        if not root.exists():
            summary["status"] = "missing"
            results.append(summary)
            continue
        if entry.unit == "dir":
            size = files = 0
            newest = 0.0
            for child in root.glob("*"):
                if child.is_symlink() or not child.is_dir():
                    continue
                child_size, child_files, child_newest, _trunc, _esc = _subtree_stats(child)
                size += child_size
                files += child_files
                newest = max(newest, child_newest)
        else:
            size = files = 0
            newest = 0.0
            for candidate in _iter_pattern_items(root, entry.patterns):
                try:
                    stat = candidate.stat()
                except OSError:
                    continue
                size += stat.st_size
                files += 1
                newest = max(newest, stat.st_mtime)
        summary["files"] = files
        summary["bytes"] = size
        summary["newest_mtime"] = newest or None
        summary["protected"] = _protected(root)
        results.append(summary)
    return results


async def _index_overview(db: AsyncSession) -> dict[str, Any]:
    size_row = await db.execute(
        text(
            """
            SELECT pg_size_pretty(pg_database_size(current_database())) AS pretty,
                   pg_database_size(current_database()) AS bytes
            """
        )
    )
    size = size_row.mappings().first() or {"pretty": "0 bytes", "bytes": 0}
    tables = await db.execute(
        text(
            """
            SELECT c.relname AS relname,
                   pg_total_relation_size(c.oid) AS bytes,
                   GREATEST(c.reltuples, 0)::bigint AS estimated_rows
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema() AND c.relkind = 'r'
            ORDER BY pg_total_relation_size(c.oid) DESC
            LIMIT 12
            """
        )
    )
    return {
        "database_bytes": int(size["bytes"] or 0),
        "database_pretty": str(size["pretty"]),
        "tables": [
            {
                "name": str(row["relname"]),
                "bytes": int(row["bytes"] or 0),
                "estimated_rows": int(row["estimated_rows"] or 0),
            }
            for row in tables.mappings()
        ],
    }


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------
def _delete_item(item: PlannedItem) -> tuple[int, int, str | None]:
    """Delete one planned path. Returns (bytes_freed, files_deleted, error)."""
    path = Path(item.path)
    entry = catalog_by_id().get(item.entry_id)
    if entry is None:
        return 0, 0, "unknown catalog entry"
    root = entry.root_path
    if not _within_root(path, root):
        return 0, 0, "path escaped its catalog root"
    if _protected(path):
        return 0, 0, "path is protected"
    try:
        if item.kind == "dir":
            if path.is_symlink() or not path.is_dir():
                return 0, 0, None
            freed, files, *_ = _subtree_stats(path)
            import shutil

            shutil.rmtree(path)
            _prune_empty_parents(path.parent, root)
            return freed, files, None
        if path.is_symlink() or not path.is_file():
            return 0, 0, None
        freed = path.stat().st_size
        path.unlink()
        _prune_empty_parents(path.parent, root)
        return freed, 1, None
    except OSError as exc:
        return 0, 0, str(exc)[:200]


def _prune_empty_parents(directory: Path, root: Path) -> None:
    current = directory
    while current != root and root in current.parents:
        try:
            next(current.iterdir())
        except StopIteration:
            try:
                current.rmdir()
            except OSError:
                return
            current = current.parent
            continue
        except OSError:
            return
        return


async def _purge_index(db: AsyncSession, plan_obj: Plan) -> dict[str, Any]:
    """Delete observatory rows that describe the removed data."""
    session_ids = await _session_ids_for_plan(db, plan_obj)
    source_ids = await _source_ids_for_plan(db, plan_obj)
    for index in range(0, len(session_ids), 200):
        chunk = session_ids[index : index + 200]
        await db.execute(delete(AgentSession).where(AgentSession.id.in_(chunk)))
    for index in range(0, len(source_ids), 500):
        chunk = source_ids[index : index + 500]
        await db.execute(delete(LocalDataSource).where(LocalDataSource.id.in_(chunk)))
    await db.flush()
    return {"sessions": len(session_ids), "sources": len(source_ids)}


async def execute_run(
    db: AsyncSession, *, run_id: str, now: datetime | None = None
) -> dict[str, Any]:
    """Apply a previously previewed retention run. Destructive."""
    now = now or datetime.now(UTC)
    run = await db.get(RetentionRun, run_id)
    if run is None:
        raise RetentionError("retention run not found")
    if run.status not in (RUN_PENDING, RUN_RUNNING):
        raise RetentionError(f"retention run is {run.status}")
    config = settings()
    if not config.retention_enabled:
        run.status = RUN_REFUSED
        run.error_kind = "disabled"
        run.error_message = "retention is disabled"
        run.finished_at = now
        await db.flush()
        return {"status": run.status}

    selection = Selection.from_payload(run.selection)
    plan_obj = plan(selection, now=now)
    await enrich_plan(db, plan_obj)
    previewed_bytes = int(run.plan.get("totals", {}).get("bytes", 0) or 0)
    drift_grace = int(getattr(config, "retention_plan_drift_bytes", 0))
    if plan_obj.bytes_total > previewed_bytes * 1.10 + drift_grace:
        run.status = RUN_REFUSED
        run.error_kind = "plan_drift"
        run.error_message = (
            f"filesystem grew past the previewed plan "
            f"({plan_obj.bytes_total} > {previewed_bytes}); preview again"
        )
        run.finished_at = now
        await db.flush()
        return {"status": run.status, "error": run.error_message}

    run.status = RUN_RUNNING
    run.started_at = now
    run.floor_at = plan_obj.floor_at
    await db.flush()

    deleted = 0
    bytes_freed = 0
    failures: list[dict[str, str]] = []
    if selection.include_raw:
        for item in plan_obj.items:
            freed, files, error = _delete_item(item)
            if error:
                failures.append({"path": item.path, "error": error})
                continue
            bytes_freed += freed
            deleted += files
    index_result = {"sessions": 0, "sources": 0}
    if selection.include_index:
        index_result = await _purge_index(db, plan_obj)

    run.status = RUN_APPLIED if not failures else RUN_FAILED
    run.bytes_freed = bytes_freed
    run.files_deleted = deleted
    run.sessions_deleted = int(index_result["sessions"])
    run.result = {
        "files_deleted": deleted,
        "bytes_freed": bytes_freed,
        "sessions_deleted": index_result["sessions"],
        "sources_deleted": index_result["sources"],
        "failure_count": len(failures),
        "failures": failures[:20],
        "plan": plan_obj.summary()["totals"],
    }
    run.finished_at = datetime.now(UTC)
    run.error_kind = "partial_failure" if failures else None
    run.error_message = (
        f"{len(failures)} path(s) could not be removed" if failures else None
    )
    await db.flush()
    await audit.record(
        db,
        action="retention.purge.applied",
        actor_type=audit.ACTOR_SYSTEM,
        resource_type="retention_run",
        resource_id=run.id,
        detail={
            "requested_by": run.requested_by,
            "keep_days": run.keep_days,
            "scope": run.scope,
            "bytes_freed": bytes_freed,
            "files_deleted": deleted,
            "sessions_deleted": index_result["sessions"],
            "sources_deleted": index_result["sources"],
            "failures": len(failures),
            "entries": sorted({item.entry_id for item in plan_obj.items}),
        },
        result=audit.RESULT_SUCCESS if not failures else audit.RESULT_FAILURE,
        error_kind="partial_failure" if failures else None,
    )
    return {"status": run.status, **run.result}


# --------------------------------------------------------------------------
# persistence helpers
# --------------------------------------------------------------------------
def new_run_id() -> str:
    import uuid

    return f"ret_{uuid.uuid4().hex[:24]}"


async def create_run(
    db: AsyncSession,
    *,
    selection: Selection,
    plan_obj: Plan,
    principal_id: str,
    scope: str,
) -> RetentionRun:
    run = RetentionRun(
        id=new_run_id(),
        requested_by=principal_id,
        scope=scope,
        status=RUN_PENDING,
        keep_days=selection.keep_days,
        floor_at=plan_obj.floor_at,
        selection=selection.to_payload(),
        plan=plan_obj.summary(),
        result={},
    )
    db.add(run)
    await db.flush()
    return run


async def list_runs(db: AsyncSession, *, limit: int = 20) -> list[RetentionRun]:
    rows = await db.scalars(
        select(RetentionRun).order_by(RetentionRun.created_at.desc()).limit(limit)
    )
    return list(rows.all())


def serialize_run(run: RetentionRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "command_id": run.command_id,
        "requested_by": run.requested_by,
        "scope": run.scope,
        "status": run.status,
        "keep_days": run.keep_days,
        "floor_at": run.floor_at.isoformat() if run.floor_at else None,
        "selection": run.selection,
        "plan": run.plan,
        "result": run.result,
        "bytes_freed": run.bytes_freed,
        "files_deleted": run.files_deleted,
        "sessions_deleted": run.sessions_deleted,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "error_kind": run.error_kind,
        "error_message": run.error_message,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


async def apply_command(factory: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Supervisor entry point: apply one ``retention.purge`` command."""
    run_id = str(payload.get("run_id") or "")
    if not run_id:
        raise RetentionError("retention.purge command has no run_id")
    async with factory() as db:
        result = await execute_run(db, run_id=run_id)
        await db.commit()
    return result


__all__ = [
    "CatalogEntry",
    "Plan",
    "PlannedItem",
    "RetentionError",
    "Selection",
    "RISK_ORDER",
    "REGENERABLE",
    "HISTORY",
    "RUNTIME",
    "RUN_APPLIED",
    "RUN_FAILED",
    "RUN_PENDING",
    "RUN_REFUSED",
    "RUN_RUNNING",
    "catalog",
    "catalog_by_id",
    "create_run",
    "enrich_plan",
    "execute_run",
    "floor_for",
    "list_runs",
    "new_run_id",
    "overview",
    "overview_generation",
    "plan",
    "serialize_run",
    "sign_plan",
    "verify_plan",
    "apply_command",
]
