"""Read-only ingestion for local agent SQLite stores and installed skills."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent_observatory.db.models import (
    ActivitySpan,
    AgentSession,
    AgentTurn,
    EvidenceRecord,
    LocalDataSource,
    SessionSkillEvidence,
    SkillDefinition,
    ThreadEdge,
    ThreadGoal,
)

_BATCH_SIZE = 250
_SKILL_PATH_RE = re.compile(
    r"(?:^|[\s\"'])[^\"'\s]*?/skills/(?:\.system/)?(?P<name>[^/\"'\s]+)/SKILL\.md"
)
_SHADOW_SKILLS_RE = re.compile(
    r"(?:selected_(?:entries|skills)|candidates?)\s*=\s*(?P<value>\[[^\]]*\]|[^\s]+)",
    re.IGNORECASE,
)
_TURN_ID_RE = re.compile(r"\bturn(?:\.id|_id)=([0-9A-Za-z-]+)")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _safe(value: Any) -> Any:
    """Make values PostgreSQL-safe without otherwise shortening evidence."""
    if isinstance(value, str):
        return value.replace("\x00", "\\u0000")
    if isinstance(value, dict):
        return {str(_safe(key)): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    return value


def _stable_id(namespace: str, value: str) -> str:
    return hashlib.sha256(f"{namespace}:{value}".encode()).hexdigest()[:48]


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def _as_datetime(value: Any, *, milliseconds: bool = False) -> datetime | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if milliseconds:
        number /= 1000
    try:
        return datetime.fromtimestamp(number, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _json_object(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else {"value": value}


def _outcome(status: str | None) -> str:
    normalized = (status or "").lower()
    if normalized in {"completed", "complete", "success", "succeeded"}:
        return "completed"
    if normalized in {"failed", "error"}:
        return "failed"
    if normalized in {"interrupted", "cancelled", "canceled"}:
        return "interrupted"
    if normalized in {"active", "running", "in_progress", "inprogress", "open"}:
        return "in_progress"
    return "unknown"


class LocalInsightsIndexer:
    """Incrementally copy auditable local Codex state into PostgreSQL."""

    def __init__(
        self,
        codex_home: str | Path,
        backfill_days: int = 90,
        parser_version: str = "1",
        agent: str = "codex",
    ) -> None:
        self.codex_home = Path(codex_home).expanduser()
        self.backfill_days = backfill_days
        self.parser_version = parser_version
        self.agent = agent
        self.cutoff = _utcnow() - timedelta(days=backfill_days)
        self.paths = {
            "thread_history": self.codex_home / "thread_history_1.sqlite",
            "state": self.codex_home / "state_5.sqlite",
            "logs": self.codex_home / "logs_2.sqlite",
            "goals": self.codex_home / "goals_1.sqlite",
            "skills": self.codex_home / "skills",
        }
        self._skill_ids: dict[str, str] = {}

    async def ingest_all(self, db: AsyncSession) -> dict[str, int]:
        """Ingest every fixed local source, isolating failures per source."""
        self.cutoff = _utcnow() - timedelta(days=self.backfill_days)
        sessions = await self._session_map(db)
        results: dict[str, int] = {}
        handlers = {
            "skills": self._ingest_skills,
            "thread_history": self._ingest_thread_history,
            "state": self._ingest_state,
            "logs": self._ingest_logs,
            "goals": self._ingest_goals,
        }
        for kind, handler in handlers.items():
            path = self.paths[kind]
            source = await self._ensure_source(db, kind, path)
            if not path.exists():
                source.status = "missing"
                source.last_attempt_at = _utcnow()
                source.last_error_kind = "FileNotFoundError"
                source.last_error = f"local source not found: {path}"
                results[kind] = 0
                await db.flush()
                continue
            fingerprint = self._fingerprint(path)
            if (
                kind in {"state", "goals", "skills"}
                and source.status == "indexed"
                and source.parser_version == self.parser_version
                and source.fingerprint_json == fingerprint
            ):
                source.last_attempt_at = _utcnow()
                results[kind] = 0
                await db.flush()
                continue
            source.status = "indexing"
            source.last_attempt_at = _utcnow()
            source.last_error_kind = None
            source.last_error = None
            await db.flush()
            try:
                async with db.begin_nested():
                    count = await handler(db, source, path, sessions)
            except Exception as exc:  # source isolation is intentional
                await db.refresh(source)
                source.status = "degraded"
                source.last_error_kind = type(exc).__name__[:64]
                source.last_error = str(exc)[:2000]
                results[kind] = 0
            else:
                source.status = "indexed"
                source.last_success_at = _utcnow()
                source.parser_version = self.parser_version
                source.fingerprint_json = self._fingerprint(path)
                source.last_error_kind = None
                source.last_error = None
                results[kind] = count
            await db.flush()
        return results

    async def _ensure_source(
        self, db: AsyncSession, kind: str, path: Path
    ) -> LocalDataSource:
        resolved = str(path.resolve(strict=False))
        identity = f"{kind}:{resolved}"
        source = await db.scalar(
            select(LocalDataSource).where(LocalDataSource.source_identity == identity)
        )
        if source is None:
            source = LocalDataSource(
                id=_stable_id("local-source", identity),
                agent=self.agent,
                source_kind=kind,
                source_identity=identity,
                source_path=resolved,
                source_format="directory" if kind == "skills" else "sqlite",
                parser_version=self.parser_version,
                cursor_json={},
                fingerprint_json={},
                backfill_cutoff_at=self.cutoff,
            )
            db.add(source)
            await db.flush()
        source.source_path = resolved
        source.backfill_cutoff_at = self.cutoff
        return source

    @staticmethod
    async def _session_map(db: AsyncSession) -> dict[str, str]:
        rows = (
            await db.execute(
                select(AgentSession.thread_id, AgentSession.id).order_by(
                    AgentSession.updated_at.desc()
                )
            )
        ).all()
        result: dict[str, str] = {}
        for thread_id, session_id in rows:
            result.setdefault(thread_id, session_id)
        return result

    @staticmethod
    def _fingerprint(path: Path) -> dict[str, Any]:
        if path.is_dir():
            files = sorted(path.rglob("SKILL.md"))
            return {
                "file_count": len(files),
                "byte_size": sum(item.stat().st_size for item in files),
                "max_mtime_ns": max(
                    (item.stat().st_mtime_ns for item in files), default=0
                ),
            }
        stat = path.stat()
        fingerprint: dict[str, Any] = {
            "byte_size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "inode": getattr(stat, "st_ino", 0),
        }
        sidecars = {}
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{path}{suffix}")
            if sidecar.exists():
                sidecar_stat = sidecar.stat()
                sidecars[suffix[1:]] = {
                    "byte_size": sidecar_stat.st_size,
                    "mtime_ns": sidecar_stat.st_mtime_ns,
                }
        if sidecars:
            fingerprint["sidecars"] = sidecars
        return fingerprint

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection

    @staticmethod
    def _require_columns(
        connection: sqlite3.Connection, table: str, required: set[str]
    ) -> set[str]:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if exists is None:
            raise RuntimeError(f"required SQLite table is missing: {table}")
        columns = {
            str(row["name"])
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        }
        missing = required - columns
        if missing:
            raise RuntimeError(
                f"SQLite table {table} lacks required columns: {sorted(missing)}"
            )
        return columns

    @staticmethod
    def _batches(cursor: sqlite3.Cursor) -> Iterator[list[sqlite3.Row]]:
        while batch := cursor.fetchmany(_BATCH_SIZE):
            yield batch

    async def _upsert_evidence(
        self, db: AsyncSession, rows: list[dict[str, Any]]
    ) -> None:
        if not rows:
            return
        statement = pg_insert(EvidenceRecord).values(rows)
        excluded = statement.excluded
        await db.execute(
            statement.on_conflict_do_update(
                constraint="uq_evidence_records_source_id",
                set_={
                    "agent_session_id": excluded.agent_session_id,
                    "thread_id": excluded.thread_id,
                    "turn_id": excluded.turn_id,
                    "recorded_at": excluded.recorded_at,
                    "category": excluded.category,
                    "level": excluded.level,
                    "target": excluded.target,
                    "summary": excluded.summary,
                    "raw_text": excluded.raw_text,
                    "raw_json": excluded.raw_json,
                    "raw_hash": excluded.raw_hash,
                    "source_locator": excluded.source_locator,
                    "metadata_json": excluded.metadata_json,
                },
            )
        )

    async def _ingest_thread_history(
        self,
        db: AsyncSession,
        source: LocalDataSource,
        path: Path,
        sessions: dict[str, str],
    ) -> int:
        cutoff_seconds = int(self.cutoff.timestamp())
        cutoff_ms = cutoff_seconds * 1000
        count = 0
        minimum: datetime | None = None
        maximum: datetime | None = None
        item_metrics: dict[tuple[str, str], dict[str, Any]] = {}
        cursor_state = source.cursor_json or {}
        previous_fingerprint = source.fingerprint_json or {}
        current_fingerprint = self._fingerprint(path)
        reset_cursor = (
            source.parser_version != self.parser_version
            or current_fingerprint.get("inode") != previous_fingerprint.get("inode")
            or int(current_fingerprint.get("byte_size", 0))
            < int(previous_fingerprint.get("byte_size", 0))
        )
        prior_turn_rowid = 0 if reset_cursor else int(cursor_state.get("turn_rowid", 0))
        prior_item_rowid = 0 if reset_cursor else int(cursor_state.get("item_rowid", 0))
        prior_item_ordinals = (
            {}
            if reset_cursor
            else {
                str(thread_id): int(value)
                for thread_id, value in (
                    cursor_state.get("item_updated_ordinals", {}) or {}
                ).items()
                if isinstance(value, int)
            }
        )
        prior_open_turns = (
            []
            if reset_cursor
            else [
                int(value)
                for value in cursor_state.get("open_turn_rowids", [])
                if isinstance(value, int)
            ]
        )
        with self._connect(path) as connection:
            turn_columns = self._require_columns(
                connection,
                "thread_turns",
                {"thread_id", "turn_id", "rollout_ordinal", "status"},
            )
            item_columns = self._require_columns(
                connection,
                "thread_items",
                {
                    "thread_id",
                    "turn_id",
                    "item_id",
                    "rollout_ordinal",
                    "created_at_ms",
                    "item_json",
                    "item_type",
                },
            )
            time_expression = (
                "COALESCE(completed_at, started_at, 0)"
                if {"started_at", "completed_at"} <= turn_columns
                else None
            )
            turn_clauses: list[str] = []
            turn_params: list[Any] = []
            if time_expression is not None:
                turn_clauses.append(f"{time_expression} >= ?")
                turn_params.append(cutoff_seconds)
            if not reset_cursor:
                delta_parts = ["rowid > ?"]
                turn_params.append(prior_turn_rowid)
                if prior_open_turns:
                    placeholders = ",".join("?" for _ in prior_open_turns)
                    delta_parts.append(f"rowid IN ({placeholders})")
                    turn_params.extend(prior_open_turns)
                turn_clauses.append("(" + " OR ".join(delta_parts) + ")")
            turns = connection.execute(
                'SELECT rowid AS "_rowid", * FROM thread_turns '
                + ("WHERE " + " AND ".join(turn_clauses) + " " if turn_clauses else "")
                + "ORDER BY rowid",
                turn_params,
            )
            max_turn_rowid = prior_turn_rowid
            open_turn_rowids: list[int] = []
            for batch in self._batches(turns):
                evidence_rows: list[dict[str, Any]] = []
                for native_row in batch:
                    row = dict(native_row)
                    thread_id = str(row["thread_id"])
                    turn_id = str(row["turn_id"])
                    session_id = sessions.get(thread_id)
                    started_at = _as_datetime(row.get("started_at"))
                    completed_at = _as_datetime(row.get("completed_at"))
                    recorded_at = completed_at or started_at
                    minimum, maximum = self._bounds(minimum, maximum, recorded_at)
                    raw = {key: value for key, value in row.items() if key != "_rowid"}
                    raw_text = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
                    record_id = f"turn:{thread_id}:{turn_id}"
                    evidence_rows.append(
                        self._evidence_values(
                            source,
                            record_id,
                            category="thread_turn",
                            raw_text=raw_text,
                            raw_json=raw,
                            recorded_at=recorded_at,
                            session_id=session_id,
                            thread_id=thread_id,
                            turn_id=turn_id,
                            summary=str(row.get("status") or "unknown"),
                            locator={"table": "thread_turns", "rowid": row["_rowid"]},
                        )
                    )
                    if session_id:
                        values: dict[str, Any] = {
                            "status": str(row.get("status") or "unknown")[:32],
                            "outcome": _outcome(row.get("status")),
                            "metric_confidence": "exact",
                        }
                        if row.get("duration_ms") is not None:
                            values["duration_ms"] = max(0, int(row["duration_ms"]))
                        if started_at is not None:
                            values["started_at"] = started_at
                        if completed_at is not None:
                            values["completed_at"] = completed_at
                        await db.execute(
                            update(AgentTurn)
                            .where(
                                AgentTurn.agent_session_id == session_id,
                                AgentTurn.turn_id == turn_id,
                            )
                            .values(**values)
                        )
                    max_turn_rowid = max(max_turn_rowid, int(row["_rowid"]))
                    if (
                        row.get("completed_at") is None
                        or str(row.get("status") or "").lower()
                        in {"active", "running", "in_progress", "inprogress", "open"}
                    ):
                        open_turn_rowids.append(int(row["_rowid"]))
                    count += 1
                await self._upsert_evidence(db, evidence_rows)

            current_item_ordinals: dict[str, int] = {}
            changed_threads: list[str] = []
            if "updated_at_ordinal" in item_columns:
                current_item_ordinals = {
                    str(row[0]): int(row[1] or 0)
                    for row in connection.execute(
                        """
                        SELECT thread_id, MAX(COALESCE(updated_at_ordinal, rollout_ordinal, 0))
                        FROM thread_items
                        WHERE created_at_ms >= ?
                        GROUP BY thread_id
                        """,
                        (cutoff_ms,),
                    )
                }
                changed_threads = [
                    thread_id
                    for thread_id, ordinal in current_item_ordinals.items()
                    if reset_cursor
                    or ordinal > int(prior_item_ordinals.get(thread_id, -1))
                ]
            item_delta = ["rowid > ?"]
            item_params: list[Any] = [cutoff_ms, prior_item_rowid]
            if changed_threads:
                placeholders = ",".join("?" for _ in changed_threads)
                item_delta.append(f"thread_id IN ({placeholders})")
                item_params.extend(changed_threads)
            items = connection.execute(
                'SELECT rowid AS "_rowid", * FROM thread_items '
                "WHERE created_at_ms >= ? AND ("
                + " OR ".join(item_delta)
                + ") ORDER BY rowid",
                item_params,
            )
            max_item_rowid = prior_item_rowid
            for batch in self._batches(items):
                evidence_rows = []
                span_rows: list[dict[str, Any]] = []
                skill_rows: list[dict[str, Any]] = []
                for native_row in batch:
                    row = dict(native_row)
                    thread_id = str(row["thread_id"])
                    turn_id = str(row["turn_id"])
                    item_id = str(row["item_id"])
                    session_id = sessions.get(thread_id)
                    recorded_at = _as_datetime(row.get("created_at_ms"), milliseconds=True)
                    minimum, maximum = self._bounds(minimum, maximum, recorded_at)
                    raw_text = str(row.get("item_json") or "")
                    raw_json = _json_object(raw_text)
                    record_id = f"item:{thread_id}:{turn_id}:{item_id}"
                    evidence_id = _stable_id(
                        "evidence", f"{source.id}:{record_id}"
                    )
                    evidence_rows.append(
                        self._evidence_values(
                            source,
                            record_id,
                            category="thread_item",
                            raw_text=raw_text,
                            raw_json=raw_json,
                            recorded_at=recorded_at,
                            session_id=session_id,
                            thread_id=thread_id,
                            turn_id=turn_id,
                            summary=str(row.get("item_type") or "unknown"),
                            locator={"table": "thread_items", "rowid": row["_rowid"]},
                            metadata={"item_type": row.get("item_type")},
                        )
                    )
                    if session_id and recorded_at and raw_json:
                        duration = self._duration_ms(raw_json)
                        if duration is not None:
                            completed_at = self._item_completed_at(raw_json, recorded_at)
                            started_at = self._item_started_at(
                                raw_json, completed_at, duration
                            )
                            span_rows.append(
                                {
                                    "id": _stable_id(
                                        "activity-span", f"{source.id}:{record_id}"
                                    ),
                                    "source_kind": "thread_history",
                                    "source_record_id": record_id,
                                    "agent": source.agent,
                                    "agent_session_id": session_id,
                                    "evidence_id": evidence_id,
                                    "thread_id": thread_id,
                                    "turn_id": turn_id,
                                    "span_type": self._span_type(
                                        str(row.get("item_type") or "")
                                    ),
                                    "name": self._span_name(raw_json),
                                    "status": str(
                                        raw_json.get("status") or "unknown"
                                    )[:32],
                                    "started_at": started_at,
                                    "completed_at": completed_at,
                                    "duration_ms": duration,
                                    "confidence": "exact",
                                    "metadata_json": _safe(
                                        {"item_type": row.get("item_type")}
                                    ),
                                }
                            )
                            metrics = item_metrics.setdefault(
                                (session_id, turn_id),
                                {"tool_intervals": [], "first_agent_ms": 0},
                            )
                            if self._span_type(str(row.get("item_type") or "")) == "tool":
                                metrics["tool_intervals"].append(
                                    (started_at, completed_at)
                                )
                        if str(row.get("item_type")) != "userMessage":
                            metrics = item_metrics.setdefault(
                                (session_id, turn_id),
                                {"tool_intervals": [], "first_agent_ms": 0},
                            )
                            timestamp_ms = int(recorded_at.timestamp() * 1000)
                            if not metrics["first_agent_ms"] or timestamp_ms < metrics[
                                "first_agent_ms"
                            ]:
                                metrics["first_agent_ms"] = timestamp_ms
                        skill_rows.extend(
                            self._skill_rows_from_item(
                                source,
                                session_id,
                                evidence_id,
                                thread_id,
                                turn_id,
                                record_id,
                                recorded_at,
                                raw_json,
                            )
                        )
                    max_item_rowid = max(max_item_rowid, int(row["_rowid"]))
                    count += 1
                await self._upsert_evidence(db, evidence_rows)
                await self._upsert_spans(db, span_rows)
                await self._upsert_skill_evidence(db, skill_rows)

            turn_count_query = "SELECT COUNT(*) FROM thread_turns"
            turn_count_params: tuple[Any, ...] = ()
            if time_expression is not None:
                turn_count_query += f" WHERE {time_expression} >= ?"
                turn_count_params = (cutoff_seconds,)
            total_turns = int(
                connection.execute(turn_count_query, turn_count_params).fetchone()[0]
            )
            total_items = int(
                connection.execute(
                    "SELECT COUNT(*) FROM thread_items WHERE created_at_ms >= ?",
                    (cutoff_ms,),
                ).fetchone()[0]
            )

        for (session_id, turn_id), metrics in item_metrics.items():
            turn = await db.scalar(
                select(AgentTurn).where(
                    AgentTurn.agent_session_id == session_id,
                    AgentTurn.turn_id == turn_id,
                )
            )
            if turn is None:
                continue
            turn.active_tool_ms = self._interval_union_ms(
                metrics["tool_intervals"]
            )
            if turn.started_at and metrics["first_agent_ms"]:
                turn.first_agent_latency_ms = max(
                    0,
                    metrics["first_agent_ms"]
                    - int(turn.started_at.timestamp() * 1000),
                )
            turn.metric_confidence = "derived"
        source.cursor_json = {
            "turn_rowid": max_turn_rowid,
            "open_turn_rowids": sorted(set(open_turn_rowids)),
            "item_rowid": max_item_rowid,
            "item_updated_ordinals": current_item_ordinals,
        }
        self._finish_source(source, path, total_turns + total_items, minimum, maximum)
        return count

    async def _ingest_state(
        self,
        db: AsyncSession,
        source: LocalDataSource,
        path: Path,
        sessions: dict[str, str],
    ) -> int:
        count = 0
        minimum: datetime | None = None
        maximum: datetime | None = None
        cutoff_ms = int(self.cutoff.timestamp() * 1000)
        cursor_state = source.cursor_json or {}
        previous_fingerprint = source.fingerprint_json or {}
        current_fingerprint = self._fingerprint(path)
        reset_cursor = (
            source.parser_version != self.parser_version
            or current_fingerprint.get("inode") != previous_fingerprint.get("inode")
            or int(current_fingerprint.get("byte_size", 0))
            < int(previous_fingerprint.get("byte_size", 0))
        )
        prior_updated = (
            0 if reset_cursor else int(cursor_state.get("max_updated_at_ms", 0))
        )
        with self._connect(path) as connection:
            thread_columns = self._require_columns(
                connection,
                "threads",
                {"id", "rollout_path", "created_at", "updated_at", "cwd", "title"},
            )
            self._require_columns(
                connection,
                "thread_spawn_edges",
                {"parent_thread_id", "child_thread_id", "status"},
            )
            updated_expression = (
                "COALESCE(updated_at_ms, updated_at * 1000)"
                if "updated_at_ms" in thread_columns
                else "updated_at * 1000"
            )
            current_filter = (
                " OR archived = 0" if "archived" in thread_columns else ""
            )
            if reset_cursor:
                thread_where = f"WHERE {updated_expression} >= ?{current_filter}"
                thread_params: tuple[Any, ...] = (cutoff_ms,)
            else:
                thread_where = f"WHERE {updated_expression} > ?"
                thread_params = (prior_updated,)
            cursor = connection.execute(
                f'SELECT rowid AS "_rowid", * FROM threads {thread_where}',
                thread_params,
            )
            max_updated = prior_updated
            for batch in self._batches(cursor):
                evidence_rows: list[dict[str, Any]] = []
                for native_row in batch:
                    row = dict(native_row)
                    thread_id = str(row["id"])
                    session_id = sessions.get(thread_id)
                    created_at = self._preferred_ms_or_seconds(
                        row.get("created_at_ms"), row.get("created_at")
                    )
                    updated_at = self._preferred_ms_or_seconds(
                        row.get("updated_at_ms"), row.get("updated_at")
                    )
                    minimum, maximum = self._bounds(
                        minimum, maximum, updated_at or created_at
                    )
                    raw = {key: value for key, value in row.items() if key != "_rowid"}
                    raw_text = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
                    evidence_rows.append(
                        self._evidence_values(
                            source,
                            f"thread:{thread_id}",
                            category="thread_state",
                            raw_text=raw_text,
                            raw_json=raw,
                            recorded_at=updated_at or created_at,
                            session_id=session_id,
                            thread_id=thread_id,
                            summary=str(row.get("title") or thread_id),
                            locator={"table": "threads", "rowid": row["_rowid"]},
                        )
                    )
                    if session_id:
                        values = {
                            key: value
                            for key, value in {
                                "cwd": row.get("cwd"),
                                "git_branch": row.get("git_branch"),
                                "originator": row.get("source"),
                                "cli_version": row.get("cli_version"),
                                "model": row.get("model") or row.get("model_provider"),
                                "thread_name": row.get("name"),
                                "approval_mode": row.get("approval_mode"),
                                "is_archived": bool(row.get("archived")),
                            }.items()
                            if value is not None
                        }
                        if values:
                            await db.execute(
                                update(AgentSession)
                                .where(AgentSession.id == session_id)
                                .values(**_safe(values))
                            )
                    max_updated = max(
                        max_updated,
                        int(row.get("updated_at_ms") or 0),
                        int(row.get("updated_at") or 0) * 1000,
                    )
                    count += 1
                await self._upsert_evidence(db, evidence_rows)

            edges = connection.execute(
                """
                SELECT edge.rowid AS "_rowid", edge.*,
                       COALESCE(child.created_at_ms, child.created_at * 1000)
                           AS "_child_created_at_ms"
                FROM thread_spawn_edges AS edge
                LEFT JOIN threads AS child ON child.id = edge.child_thread_id
                """
            )
            for batch in self._batches(edges):
                rows = []
                for native_row in batch:
                    row = dict(native_row)
                    parent = str(row["parent_thread_id"])
                    child = str(row["child_thread_id"])
                    rows.append(
                        {
                            "id": _stable_id("thread-edge", f"{parent}:{child}"),
                            "source_id": source.id,
                            "parent_thread_id": parent,
                            "child_thread_id": child,
                            "status": str(row.get("status") or "unknown")[:32],
                            "event_time": _as_datetime(
                                row.get("_child_created_at_ms"),
                                milliseconds=True,
                            ),
                            "metadata_json": {
                                "source_locator": {
                                    "table": "thread_spawn_edges",
                                    "rowid": row["_rowid"],
                                }
                            },
                        }
                    )
                    count += 1
                await self._upsert_edges(db, rows)
            total_threads = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM threads "
                    f"WHERE {updated_expression} >= ?{current_filter}",
                    (cutoff_ms,),
                ).fetchone()[0]
            )
            total_edges = int(
                connection.execute(
                    "SELECT COUNT(*) FROM thread_spawn_edges"
                ).fetchone()[0]
            )
        source.cursor_json = {"max_updated_at_ms": max_updated}
        self._finish_source(
            source, path, total_threads + total_edges, minimum, maximum
        )
        return count

    async def _ingest_logs(
        self,
        db: AsyncSession,
        source: LocalDataSource,
        path: Path,
        sessions: dict[str, str],
    ) -> int:
        cutoff_seconds = int(self.cutoff.timestamp())
        prior_fingerprint = source.fingerprint_json or {}
        current_fingerprint = self._fingerprint(path)
        last_id = int((source.cursor_json or {}).get("last_id", 0))
        reset_cursor = (
            source.parser_version != self.parser_version
            or int(current_fingerprint.get("byte_size", 0))
            < int(prior_fingerprint.get("byte_size", 0))
            or current_fingerprint.get("inode") != prior_fingerprint.get("inode")
        )
        if reset_cursor:
            last_id = 0
        count = 0
        maximum_id = last_id
        minimum: datetime | None = None
        maximum: datetime | None = None
        with self._connect(path) as connection:
            self._require_columns(
                connection,
                "logs",
                {"id", "ts", "level", "target", "feedback_log_body", "thread_id"},
            )
            cursor = connection.execute(
                "SELECT * FROM logs WHERE id > ? AND ts >= ? ORDER BY id",
                (last_id, cutoff_seconds),
            )
            source_maximum_id = int(
                connection.execute("SELECT COALESCE(MAX(id), 0) FROM logs").fetchone()[0]
            )
            for batch in self._batches(cursor):
                evidence_rows: list[dict[str, Any]] = []
                skill_rows: list[dict[str, Any]] = []
                for native_row in batch:
                    row = dict(native_row)
                    log_id = int(row["id"])
                    thread_id = (
                        str(row["thread_id"]) if row.get("thread_id") else None
                    )
                    session_id = sessions.get(thread_id) if thread_id else None
                    recorded_at = _as_datetime(row.get("ts"))
                    minimum, maximum = self._bounds(minimum, maximum, recorded_at)
                    body = str(row.get("feedback_log_body") or "")
                    record_id = f"log:{log_id}"
                    evidence_id = _stable_id(
                        "evidence", f"{source.id}:{record_id}"
                    )
                    turn_match = _TURN_ID_RE.search(body)
                    turn_id = turn_match.group(1) if turn_match else None
                    evidence_rows.append(
                        self._evidence_values(
                            source,
                            record_id,
                            category=(
                                "skill_candidate"
                                if "shadow_selection" in str(row.get("target") or "")
                                else "log"
                            ),
                            raw_text=body,
                            raw_json=row,
                            recorded_at=recorded_at,
                            session_id=session_id,
                            thread_id=thread_id,
                            turn_id=turn_id,
                            summary=" ".join(
                                part
                                for part in (
                                    str(row.get("level") or ""),
                                    str(row.get("target") or ""),
                                )
                                if part
                            )[:500],
                            locator={"table": "logs", "id": log_id},
                            level=(
                                str(row["level"])[:32] if row.get("level") else None
                            ),
                            target=(
                                str(row["target"]) if row.get("target") else None
                            ),
                            metadata={
                                "ts_nanos": row.get("ts_nanos"),
                            },
                        )
                    )
                    if session_id and "shadow_selection" in str(
                        row.get("target") or ""
                    ):
                        for skill_name in self._shadow_skill_names(body):
                            skill_rows.append(
                                self._skill_evidence_values(
                                    session_id=session_id,
                                    skill_id=self._skill_ids.get(skill_name),
                                    evidence_id=evidence_id,
                                    turn_id=turn_id,
                                    skill_name=skill_name,
                                    evidence_type="candidate",
                                    confidence="exact",
                                    source_record_id=record_id,
                                    event_time=recorded_at,
                                    metadata={
                                        "source": "shadow_selection",
                                        "never_implies_used": True,
                                    },
                                )
                            )
                    maximum_id = max(maximum_id, log_id)
                    count += 1
                await self._upsert_evidence(db, evidence_rows)
                await self._upsert_skill_evidence(db, skill_rows)
        source.cursor_json = {"last_id": max(maximum_id, source_maximum_id)}
        base_count = 0 if reset_cursor else source.record_count
        self._finish_source(source, path, base_count + count, minimum, maximum)
        return count

    async def _ingest_goals(
        self,
        db: AsyncSession,
        source: LocalDataSource,
        path: Path,
        sessions: dict[str, str],
    ) -> int:
        del sessions
        count = 0
        minimum: datetime | None = None
        maximum: datetime | None = None
        max_updated = 0
        cutoff_ms = int(self.cutoff.timestamp() * 1000)
        with self._connect(path) as connection:
            self._require_columns(
                connection,
                "thread_goals",
                {
                    "thread_id",
                    "goal_id",
                    "objective",
                    "status",
                    "created_at_ms",
                    "updated_at_ms",
                },
            )
            cursor = connection.execute(
                'SELECT rowid AS "_rowid", * FROM thread_goals '
                "WHERE updated_at_ms >= ? OR lower(status) NOT IN "
                "('complete', 'completed', 'failed', 'cancelled', 'canceled')",
                (cutoff_ms,),
            )
            for batch in self._batches(cursor):
                rows = []
                evidence_rows = []
                for native_row in batch:
                    row = dict(native_row)
                    thread_id = str(row["thread_id"])
                    created_at = _as_datetime(
                        row.get("created_at_ms"), milliseconds=True
                    )
                    updated_at = _as_datetime(
                        row.get("updated_at_ms"), milliseconds=True
                    )
                    minimum, maximum = self._bounds(
                        minimum, maximum, updated_at or created_at
                    )
                    rows.append(
                        {
                            "thread_id": thread_id,
                            "source_id": source.id,
                            "goal_id": str(row["goal_id"]),
                            "objective": _safe(str(row["objective"])),
                            "status": str(row["status"])[:32],
                            "token_budget": row.get("token_budget"),
                            "tokens_used": int(row.get("tokens_used") or 0),
                            "time_used_seconds": int(
                                row.get("time_used_seconds") or 0
                            ),
                            "source_created_at_ms": int(row["created_at_ms"]),
                            "updated_at_ms": int(row["updated_at_ms"]),
                        }
                    )
                    raw = {key: value for key, value in row.items() if key != "_rowid"}
                    raw_text = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
                    evidence_rows.append(
                        self._evidence_values(
                            source,
                            f"goal:{thread_id}",
                            category="thread_goal",
                            raw_text=raw_text,
                            raw_json=raw,
                            recorded_at=updated_at,
                            session_id=None,
                            thread_id=thread_id,
                            summary=str(row["status"]),
                            locator={"table": "thread_goals", "rowid": row["_rowid"]},
                        )
                    )
                    max_updated = max(max_updated, int(row["updated_at_ms"]))
                    count += 1
                await self._upsert_goals(db, rows)
                await self._upsert_evidence(db, evidence_rows)
        source.cursor_json = {"max_updated_at_ms": max_updated}
        self._finish_source(source, path, count, minimum, maximum)
        return count

    async def _ingest_skills(
        self,
        db: AsyncSession,
        source: LocalDataSource,
        path: Path,
        sessions: dict[str, str],
    ) -> int:
        del sessions
        now = _utcnow()
        rows = []
        evidence_rows = []
        max_mtime_ns = 0
        files = sorted(path.rglob("SKILL.md"))
        seen_paths: set[str] = set()
        for skill_path in files:
            raw_bytes = skill_path.read_bytes()
            raw_text = raw_bytes.decode("utf-8", errors="replace")
            name, description = self._frontmatter(raw_text)
            name = name or skill_path.parent.name
            resolved = str(skill_path.resolve())
            seen_paths.add(resolved)
            content_hash = hashlib.sha256(raw_bytes).hexdigest()
            skill_id = _stable_id("skill", resolved)
            scope = "system" if ".system" in skill_path.parts else "user"
            rows.append(
                {
                    "id": skill_id,
                    "name": _safe(name)[:255],
                    "description": _safe(description) if description else None,
                    "source_path": resolved,
                    "scope": scope,
                    "content_hash": content_hash,
                    "enabled": True,
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "metadata_json": {
                        "relative_path": str(skill_path.relative_to(path))
                    },
                }
            )
            self._skill_ids[name] = skill_id
            evidence_rows.append(
                self._evidence_values(
                    source,
                    f"skill:{_stable_id('skill-path', resolved)}:{content_hash}",
                    category="skill_definition",
                    raw_text=raw_text,
                    raw_json={
                        "name": name,
                        "description": description,
                        "scope": scope,
                    },
                    recorded_at=datetime.fromtimestamp(
                        skill_path.stat().st_mtime, tz=UTC
                    ),
                    session_id=None,
                    thread_id=None,
                    summary=name,
                    locator={"path": resolved},
                    metadata={"content_hash": content_hash},
                )
            )
            max_mtime_ns = max(max_mtime_ns, skill_path.stat().st_mtime_ns)
        disable_query = update(SkillDefinition).where(
            SkillDefinition.source_path.startswith(f"{path.resolve()}/")
        )
        if seen_paths:
            disable_query = disable_query.where(
                SkillDefinition.source_path.not_in(seen_paths)
            )
        await db.execute(disable_query.values(enabled=False))
        await self._upsert_skills(db, rows)
        await self._upsert_evidence(db, evidence_rows)
        source.cursor_json = {
            "file_count": len(files),
            "max_mtime_ns": max_mtime_ns,
        }
        fingerprint = self._fingerprint(path)
        source.byte_size = int(fingerprint["byte_size"])
        source.record_count = len(files)
        source.coverage_started_at = now if files else None
        source.coverage_ended_at = now if files else None
        return len(files)

    @staticmethod
    def _evidence_values(
        source: LocalDataSource,
        record_id: str,
        *,
        category: str,
        raw_text: str,
        raw_json: dict[str, Any] | None,
        recorded_at: datetime | None,
        session_id: str | None,
        thread_id: str | None,
        summary: str | None,
        locator: dict[str, Any],
        turn_id: str | None = None,
        level: str | None = None,
        target: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "id": _stable_id("evidence", f"{source.id}:{record_id}"),
            "source_id": source.id,
            "source_record_id": record_id[:255],
            "agent_session_id": session_id,
            "agent_event_id": None,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "recorded_at": recorded_at,
            "category": category,
            "level": level,
            "target": _safe(target) if target else None,
            "summary": _safe(summary) if summary else None,
            "raw_text": _safe(raw_text),
            "raw_json": _safe(raw_json) if raw_json is not None else None,
            "raw_hash": _hash_text(raw_text),
            "source_locator": _safe(locator),
            "metadata_json": _safe(metadata or {}),
        }

    @staticmethod
    async def _upsert_spans(
        db: AsyncSession, rows: list[dict[str, Any]]
    ) -> None:
        if not rows:
            return
        statement = pg_insert(ActivitySpan).values(rows)
        excluded = statement.excluded
        await db.execute(
            statement.on_conflict_do_update(
                index_elements=[ActivitySpan.id],
                set_={
                    "agent": excluded.agent,
                    "source_kind": excluded.source_kind,
                    "source_record_id": excluded.source_record_id,
                    "evidence_id": excluded.evidence_id,
                    "status": excluded.status,
                    "started_at": excluded.started_at,
                    "completed_at": excluded.completed_at,
                    "duration_ms": excluded.duration_ms,
                    "confidence": excluded.confidence,
                    "metadata_json": excluded.metadata_json,
                },
            )
        )

    @staticmethod
    async def _upsert_edges(
        db: AsyncSession, rows: list[dict[str, Any]]
    ) -> None:
        if not rows:
            return
        statement = pg_insert(ThreadEdge).values(rows)
        excluded = statement.excluded
        await db.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    ThreadEdge.parent_thread_id,
                    ThreadEdge.child_thread_id,
                ],
                set_={
                    "source_id": excluded.source_id,
                    "status": excluded.status,
                    "event_time": excluded.event_time,
                    "metadata_json": excluded.metadata_json,
                },
            )
        )

    @staticmethod
    async def _upsert_goals(
        db: AsyncSession, rows: list[dict[str, Any]]
    ) -> None:
        if not rows:
            return
        statement = pg_insert(ThreadGoal).values(rows)
        excluded = statement.excluded
        await db.execute(
            statement.on_conflict_do_update(
                index_elements=[ThreadGoal.thread_id],
                set_={
                    "source_id": excluded.source_id,
                    "goal_id": excluded.goal_id,
                    "objective": excluded.objective,
                    "status": excluded.status,
                    "token_budget": excluded.token_budget,
                    "tokens_used": excluded.tokens_used,
                    "time_used_seconds": excluded.time_used_seconds,
                    "source_created_at_ms": excluded.source_created_at_ms,
                    "updated_at_ms": excluded.updated_at_ms,
                },
            )
        )

    @staticmethod
    async def _upsert_skills(
        db: AsyncSession, rows: list[dict[str, Any]]
    ) -> None:
        if not rows:
            return
        statement = pg_insert(SkillDefinition).values(rows)
        excluded = statement.excluded
        await db.execute(
            statement.on_conflict_do_update(
                index_elements=[SkillDefinition.source_path],
                set_={
                    "name": excluded.name,
                    "description": excluded.description,
                    "scope": excluded.scope,
                    "content_hash": excluded.content_hash,
                    "enabled": True,
                    "last_seen_at": excluded.last_seen_at,
                    "metadata_json": excluded.metadata_json,
                },
            )
        )

    @staticmethod
    async def _upsert_skill_evidence(
        db: AsyncSession, rows: list[dict[str, Any]]
    ) -> None:
        if not rows:
            return
        statement = pg_insert(SessionSkillEvidence).values(rows)
        excluded = statement.excluded
        await db.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    SessionSkillEvidence.agent_session_id,
                    SessionSkillEvidence.skill_name,
                    SessionSkillEvidence.evidence_type,
                    SessionSkillEvidence.source_record_id,
                ],
                set_={
                    "skill_id": excluded.skill_id,
                    "evidence_id": excluded.evidence_id,
                    "turn_id": excluded.turn_id,
                    "confidence": excluded.confidence,
                    "event_time": excluded.event_time,
                    "metadata_json": excluded.metadata_json,
                },
            )
        )

    @staticmethod
    def _skill_evidence_values(
        *,
        session_id: str,
        skill_id: str | None,
        evidence_id: str,
        turn_id: str | None,
        skill_name: str,
        evidence_type: str,
        confidence: str,
        source_record_id: str,
        event_time: datetime | None,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        identity = (
            f"{session_id}:{skill_name}:{evidence_type}:{source_record_id}"
        )
        return {
            "id": _stable_id("session-skill", identity),
            "agent_session_id": session_id,
            "skill_id": skill_id,
            "evidence_id": evidence_id,
            "turn_id": turn_id,
            "skill_name": _safe(skill_name)[:255],
            "evidence_type": evidence_type,
            "confidence": confidence,
            "source_record_id": source_record_id[:255],
            "event_time": event_time,
            "metadata_json": _safe(metadata),
        }

    def _skill_rows_from_item(
        self,
        source: LocalDataSource,
        session_id: str,
        evidence_id: str,
        thread_id: str,
        turn_id: str,
        record_id: str,
        event_time: datetime,
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        command_text = str(payload.get("command") or "")
        searchable = " ".join(
            str(payload.get(key) or "")
            for key in ("command", "scriptPath", "path", "aggregatedOutput")
        )
        explicit_names = {
            match.group("name")
            for match in _SKILL_PATH_RE.finditer(command_text)
        }
        successful = (
            payload.get("exit_code") in {None, 0}
            and str(payload.get("status") or "").lower()
            not in {"failed", "error", "declined"}
        )
        rows = []
        seen: set[str] = set()
        for match in _SKILL_PATH_RE.finditer(searchable):
            name = match.group("name")
            if name in seen:
                continue
            seen.add(name)
            explicit_load = successful and name in explicit_names
            rows.append(
                self._skill_evidence_values(
                    session_id=session_id,
                    skill_id=self._skill_ids.get(name),
                    evidence_id=evidence_id,
                    turn_id=turn_id,
                    skill_name=name,
                    evidence_type="loaded" if explicit_load else "inferred",
                    confidence="exact" if explicit_load else "inferred",
                    source_record_id=record_id,
                    event_time=event_time,
                    metadata={
                        "thread_id": thread_id,
                        "source_id": source.id,
                        "reason": (
                            "successful command explicitly read SKILL.md"
                            if explicit_load
                            else "SKILL.md path appeared in a thread item"
                        ),
                    },
                )
            )
        return rows

    @staticmethod
    def _shadow_skill_names(body: str) -> list[str]:
        match = _SHADOW_SKILLS_RE.search(body)
        if match is None:
            return []
        raw = match.group("value").strip()
        if raw.startswith("["):
            try:
                values = json.loads(raw.replace("'", '"'))
            except json.JSONDecodeError:
                values = [item.strip(" \"'") for item in raw[1:-1].split(",")]
        else:
            values = [raw.strip(" \"'")]
        return list(
            dict.fromkeys(
                str(value)
                for value in values
                if str(value) and not str(value).isdigit()
            )
        )

    @staticmethod
    def _frontmatter(raw_text: str) -> tuple[str | None, str | None]:
        lines = raw_text.splitlines()
        if not lines or lines[0].strip() != "---":
            return None, None
        values: dict[str, str] = {}
        index = 1
        while index < len(lines) and lines[index].strip() != "---":
            line = lines[index]
            if ":" not in line:
                index += 1
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if key not in {"name", "description"}:
                index += 1
                continue
            if value in {"|", ">"}:
                parts = []
                index += 1
                while index < len(lines) and (
                    not lines[index].strip()
                    or lines[index].startswith((" ", "\t"))
                ):
                    if lines[index].strip():
                        parts.append(lines[index].strip())
                    index += 1
                values[key] = (
                    "\n".join(parts) if value == "|" else " ".join(parts)
                )
                continue
            values[key] = value.strip("\"'")
            index += 1
        return values.get("name"), values.get("description")

    @staticmethod
    def _duration_ms(payload: dict[str, Any]) -> int | None:
        value = payload.get("durationMs", payload.get("duration_ms"))
        if value is None and isinstance(payload.get("duration"), dict):
            duration = payload["duration"]
            try:
                value = (
                    int(duration.get("secs") or 0) * 1000
                    + int(duration.get("nanos") or 0) // 1_000_000
                )
            except (TypeError, ValueError):
                value = None
        try:
            return max(0, int(value)) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _item_completed_at(
        payload: dict[str, Any], fallback: datetime
    ) -> datetime:
        for key, milliseconds in (
            ("completedAtMs", True),
            ("completed_at_ms", True),
            ("completedAt", False),
            ("completed_at", False),
        ):
            value = _as_datetime(payload.get(key), milliseconds=milliseconds)
            if value is not None:
                return value
        return fallback

    @staticmethod
    def _item_started_at(
        payload: dict[str, Any], completed_at: datetime, duration_ms: int
    ) -> datetime:
        for key, milliseconds in (
            ("startedAtMs", True),
            ("started_at_ms", True),
            ("startedAt", False),
            ("started_at", False),
        ):
            value = _as_datetime(payload.get(key), milliseconds=milliseconds)
            if value is not None:
                return value
        return completed_at - timedelta(milliseconds=duration_ms)

    @staticmethod
    def _span_type(item_type: str) -> str:
        normalized = item_type.lower()
        if normalized in {"commandexecution", "collabagenttoolcall"}:
            return "tool"
        if normalized == "websearch":
            return "web_search"
        if normalized == "filechange":
            return "file_change"
        if normalized == "subagentactivity":
            return "subagent"
        if normalized == "reasoning":
            return "reasoning"
        if normalized == "agentmessage":
            return "agent_message"
        if normalized == "contextcompaction":
            return "context_compaction"
        if normalized == "imageview":
            return "image_view"
        return "activity"

    @staticmethod
    def _span_name(payload: dict[str, Any]) -> str | None:
        for key in ("tool", "command", "name", "type"):
            value = payload.get(key)
            if value:
                return str(value)[:255]
        return None

    @staticmethod
    def _interval_union_ms(
        intervals: list[tuple[datetime, datetime]],
    ) -> int:
        if not intervals:
            return 0
        ordered = sorted(intervals)
        start, end = ordered[0]
        total = 0
        for next_start, next_end in ordered[1:]:
            if next_start <= end:
                end = max(end, next_end)
                continue
            total += max(0, round((end - start).total_seconds() * 1000))
            start, end = next_start, next_end
        return total + max(0, round((end - start).total_seconds() * 1000))

    @staticmethod
    def _preferred_ms_or_seconds(
        milliseconds: Any, seconds: Any
    ) -> datetime | None:
        return _as_datetime(milliseconds, milliseconds=True) or _as_datetime(seconds)

    @staticmethod
    def _bounds(
        minimum: datetime | None,
        maximum: datetime | None,
        value: datetime | None,
    ) -> tuple[datetime | None, datetime | None]:
        if value is None:
            return minimum, maximum
        return (
            value if minimum is None or value < minimum else minimum,
            value if maximum is None or value > maximum else maximum,
        )

    @staticmethod
    def _finish_source(
        source: LocalDataSource,
        path: Path,
        record_count: int,
        minimum: datetime | None,
        maximum: datetime | None,
    ) -> None:
        source.record_count = record_count
        source.byte_size = path.stat().st_size
        if minimum is not None:
            source.coverage_started_at = (
                minimum
                if source.coverage_started_at is None
                else min(source.coverage_started_at, minimum)
            )
        if maximum is not None:
            source.coverage_ended_at = (
                maximum
                if source.coverage_ended_at is None
                else max(source.coverage_ended_at, maximum)
            )


__all__ = ["LocalInsightsIndexer"]
