"""Read-only indexing of CLI-managed memory stores."""
from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent_observatory.db.models import (
    AgentSession,
    LocalDataSource,
    MemoryEntry,
)
from agent_observatory.services.redaction import redact_secrets

_BATCH_SIZE = 250
_TEXT_SUFFIXES = {".md", ".txt"}
_SENSITIVE_NAME_RE = re.compile(
    r"(?:^|[-_.])(token|secret|password|credential|api[-_]?key)(?:[-_.]|$)",
    re.IGNORECASE,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _stable_id(namespace: str, value: str) -> str:
    return hashlib.sha256(f"{namespace}:{value}".encode()).hexdigest()[:48]


def _source_time(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        number = float(value)
        if number > 100_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _summary(content: str, fallback: str) -> str:
    for raw_line in content.splitlines():
        line = raw_line.strip().lstrip("#").strip()
        if line:
            return line[:500]
    return fallback[:500]


def _sensitivity(path: Path, content: str) -> str:
    if _SENSITIVE_NAME_RE.search(path.name):
        return "sensitive"
    return "sensitive" if redact_secrets(content) != content else "normal"


def _read_text(path: Path) -> str:
    chunks: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        while chunk := handle.read(64 * 1024):
            chunks.append(chunk)
    return "".join(chunks)


class MemoryIndexer:
    """Copy Codex, StepCode, and Claude memory entries into PostgreSQL."""

    parser_version = "1"

    def __init__(
        self,
        *,
        codex_home: str | Path,
        stepcode_home: str | Path,
        claude_projects_root: str | Path,
    ) -> None:
        self.sources = (
            (
                "codex",
                "codex_memories_sqlite",
                Path(codex_home).expanduser() / "memories_1.sqlite",
                "sqlite",
            ),
            (
                "stepcode",
                "stepcode_memories_sqlite",
                Path(stepcode_home).expanduser() / "memories_1.sqlite",
                "sqlite",
            ),
            (
                "claude",
                "claude_memory_files",
                Path(claude_projects_root).expanduser(),
                "directory",
            ),
        )

    async def ingest_all(self, db: AsyncSession) -> dict[str, int]:
        results: dict[str, int] = {}
        for agent, kind, path, source_format in self.sources:
            source = await self._ensure_source(
                db, agent=agent, kind=kind, path=path, source_format=source_format
            )
            source.last_attempt_at = _utcnow()
            if not path.exists():
                source.status = "missing"
                source.last_error_kind = "FileNotFoundError"
                source.last_error = f"memory source not found: {path}"
                await db.execute(
                    update(MemoryEntry)
                    .where(MemoryEntry.source_id == source.id)
                    .values(is_present=False)
                )
                results[kind] = 0
                await db.flush()
                continue

            fingerprint = self._fingerprint(kind, path)
            if (
                source.status == "indexed"
                and source.parser_version == self.parser_version
                and source.fingerprint_json == fingerprint
            ):
                results[kind] = 0
                await db.flush()
                continue

            source.status = "indexing"
            source.last_error_kind = None
            source.last_error = None
            await db.flush()
            try:
                async with db.begin_nested():
                    if source_format == "sqlite":
                        count = await self._ingest_sqlite(db, source, path)
                    else:
                        count = await self._ingest_claude(db, source, path)
            except Exception as exc:  # source failures must remain isolated
                await db.refresh(source)
                source.status = "degraded"
                source.last_error_kind = type(exc).__name__[:64]
                source.last_error = str(exc)[:2000]
                results[kind] = 0
            else:
                source.status = "indexed"
                source.parser_version = self.parser_version
                source.fingerprint_json = fingerprint
                source.last_success_at = _utcnow()
                source.last_error_kind = None
                source.last_error = None
                results[kind] = count
            await db.flush()
        return results

    async def _ensure_source(
        self,
        db: AsyncSession,
        *,
        agent: str,
        kind: str,
        path: Path,
        source_format: str,
    ) -> LocalDataSource:
        resolved = str(path.resolve(strict=False))
        identity = f"{kind}:{resolved}"
        source = await db.scalar(
            select(LocalDataSource).where(LocalDataSource.source_identity == identity)
        )
        if source is None:
            source = LocalDataSource(
                id=_stable_id("memory-source", identity),
                agent=agent,
                source_kind=kind,
                source_identity=identity,
                source_path=resolved,
                source_format=source_format,
                parser_version=self.parser_version,
                cursor_json={},
                fingerprint_json={},
            )
            db.add(source)
            await db.flush()
        source.source_path = resolved
        return source

    def _fingerprint(self, kind: str, path: Path) -> dict[str, Any]:
        if kind != "claude_memory_files":
            stat = path.stat()
            return {"byte_size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        files = list(self._claude_files(path))
        return {
            "file_count": len(files),
            "byte_size": sum(item.stat().st_size for item in files),
            "max_mtime_ns": max(
                (item.stat().st_mtime_ns for item in files), default=0
            ),
        }

    @staticmethod
    def _claude_files(root: Path) -> Iterator[Path]:
        for memory_dir in sorted(root.glob("*/memory")):
            if not memory_dir.is_dir() or memory_dir.is_symlink():
                continue
            for path in sorted(memory_dir.iterdir()):
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and path.suffix.lower() in _TEXT_SUFFIXES
                ):
                    yield path

    async def _ingest_sqlite(
        self, db: AsyncSession, source: LocalDataSource, path: Path
    ) -> int:
        now = _utcnow()
        rows: list[dict[str, Any]] = []
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            table = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='stage1_outputs'"
            ).fetchone()
            if table is not None:
                cursor = connection.execute(
                    "SELECT thread_id, source_updated_at, raw_memory, "
                    "rollout_summary, rollout_slug, generated_at, usage_count, "
                    "last_usage, selected_for_phase2 "
                    "FROM stage1_outputs ORDER BY thread_id"
                )
                while batch := cursor.fetchmany(_BATCH_SIZE):
                    for record in batch:
                        content = str(record["raw_memory"] or "")
                        thread_id = str(record["thread_id"])
                        title = str(record["rollout_slug"] or thread_id)
                        rows.append(
                            self._entry_values(
                                source=source,
                                record_id=thread_id,
                                source_path=str(path.resolve()),
                                thread_id=thread_id,
                                scope="thread",
                                title=title,
                                content=content,
                                summary=str(record["rollout_summary"] or "")
                                or _summary(content, title),
                                source_updated_at=_source_time(
                                    record["source_updated_at"]
                                    or record["generated_at"]
                                ),
                                sensitivity=_sensitivity(path, content),
                                metadata={
                                    "table": "stage1_outputs",
                                    "generated_at": record["generated_at"],
                                    "usage_count": record["usage_count"],
                                    "last_usage": record["last_usage"],
                                    "selected_for_phase2": bool(
                                        record["selected_for_phase2"]
                                    ),
                                },
                                workspace_id=None,
                                now=now,
                            )
                        )
        await self._replace_present_entries(db, source, rows)
        self._finish_source(source, path, rows, now)
        return len(rows)

    async def _ingest_claude(
        self, db: AsyncSession, source: LocalDataSource, root: Path
    ) -> int:
        now = _utcnow()
        workspace_map = await self._claude_workspace_map(db)
        rows: list[dict[str, Any]] = []
        for path in self._claude_files(root):
            content = _read_text(path)
            project_dir = path.parent.parent.resolve()
            relative = str(path.resolve().relative_to(root.resolve()))
            is_index = path.name.casefold() == "memory.md"
            title = (
                f"{project_dir.name} memory index"
                if is_index
                else path.stem.replace("-", " ").replace("_", " ")
            )
            rows.append(
                self._entry_values(
                    source=source,
                    record_id=relative,
                    source_path=str(path.resolve()),
                    thread_id=None,
                    scope="project",
                    title=title,
                    content=content,
                    summary=_summary(content, title),
                    source_updated_at=datetime.fromtimestamp(
                        path.stat().st_mtime, tz=UTC
                    ),
                    sensitivity=_sensitivity(path, content),
                    metadata={
                        "project_key": project_dir.name,
                        "relative_path": relative,
                        "is_index": is_index,
                    },
                    workspace_id=workspace_map.get(str(project_dir)),
                    now=now,
                )
            )
        await self._replace_present_entries(db, source, rows)
        self._finish_source(source, root, rows, now)
        return len(rows)

    @staticmethod
    async def _claude_workspace_map(db: AsyncSession) -> dict[str, str]:
        sessions = list(
            (
                await db.scalars(
                    select(AgentSession)
                    .where(
                        AgentSession.agent.in_(("claude", "stepcode")),
                        AgentSession.workspace_id.is_not(None),
                    )
                    .order_by(AgentSession.updated_at.desc())
                )
            ).all()
        )
        result: dict[str, str] = {}
        for session in sessions:
            if session.source_path and session.workspace_id:
                result.setdefault(
                    str(Path(session.source_path).resolve().parent),
                    session.workspace_id,
                )
        return result

    async def _replace_present_entries(
        self,
        db: AsyncSession,
        source: LocalDataSource,
        rows: list[dict[str, Any]],
    ) -> None:
        await db.execute(
            update(MemoryEntry)
            .where(MemoryEntry.source_id == source.id)
            .values(is_present=False)
        )
        if not rows:
            return
        statement = pg_insert(MemoryEntry).values(rows)
        excluded = statement.excluded
        await db.execute(
            statement.on_conflict_do_update(
                index_elements=["source_id", "source_record_id"],
                set_={
                    "workspace_id": excluded.workspace_id,
                    "agent": excluded.agent,
                    "source_kind": excluded.source_kind,
                    "source_path": excluded.source_path,
                    "thread_id": excluded.thread_id,
                    "scope": excluded.scope,
                    "title": excluded.title,
                    "content": excluded.content,
                    "summary": excluded.summary,
                    "content_hash": excluded.content_hash,
                    "source_updated_at": excluded.source_updated_at,
                    "last_seen_at": excluded.last_seen_at,
                    "is_present": True,
                    "sensitivity": excluded.sensitivity,
                    "metadata_json": excluded.metadata_json,
                    "updated_at": _utcnow(),
                },
            )
        )

    @staticmethod
    def _entry_values(
        *,
        source: LocalDataSource,
        record_id: str,
        source_path: str,
        thread_id: str | None,
        scope: str,
        title: str,
        content: str,
        summary: str,
        source_updated_at: datetime | None,
        sensitivity: str,
        metadata: dict[str, Any],
        workspace_id: str | None,
        now: datetime,
    ) -> dict[str, Any]:
        identity = f"{source.source_identity}:{record_id}"
        return {
            "id": _stable_id("memory-entry", identity),
            "source_id": source.id,
            "workspace_id": workspace_id,
            "agent": source.agent,
            "source_kind": source.source_kind,
            "source_record_id": record_id[:255],
            "source_path": source_path,
            "thread_id": thread_id,
            "scope": scope,
            "title": title[:512],
            "content": content,
            "summary": summary,
            "content_hash": hashlib.sha256(
                content.encode("utf-8", errors="surrogatepass")
            ).hexdigest(),
            "source_updated_at": source_updated_at,
            "last_seen_at": now,
            "is_present": True,
            "sensitivity": sensitivity,
            "metadata_json": metadata,
        }

    def _finish_source(
        self,
        source: LocalDataSource,
        path: Path,
        rows: list[dict[str, Any]],
        now: datetime,
    ) -> None:
        source.record_count = len(rows)
        source.byte_size = (
            path.stat().st_size
            if path.is_file()
            else sum(
                item.stat().st_size
                for item in self._claude_files(path)
            )
        )
        timestamps = [
            row["source_updated_at"]
            for row in rows
            if row["source_updated_at"] is not None
        ]
        source.coverage_started_at = min(timestamps) if timestamps else None
        source.coverage_ended_at = max(timestamps) if timestamps else None
        source.cursor_json = {"scanned_at": now.isoformat(), "record_count": len(rows)}


__all__ = ["MemoryIndexer"]
