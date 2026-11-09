"""Read-only StepGo host-run logs and native-session linking.

StepGo logs are launch/process evidence, not a second copy of the underlying
agent conversation.  The adapter therefore stores host events separately and
only links them to a native :class:`AgentSession` when the log carries an
explicit session id or transcript path.  No timestamp-based guessing is used.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_observatory.db.models import AgentSession, StepGoRun, StepGoRunEvent

from .adapters import PlanAction, SourceDescriptor, SourcePlan
from .fingerprints import file_fingerprint, indexed_content_matches
from .pi_parser import iter_records

_KNOWN_HOST_EVENTS = {
    "session.start",
    "session.end",
    "stream",
    "transcript",
    "turn_end",
    "session_start",
    "session_end",
    "status",
    "error",
}
_NATIVE_ID_KEYS = {
    "nativesessionid",
    "native_session_id",
    "agentsessionid",
    "agent_session_id",
    "threadid",
    "thread_id",
}
_TRANSCRIPT_KEYS = {
    "transcriptpath",
    "transcript_path",
    "rolloutpath",
    "rollout_path",
}


def _id(prefix: str, value: str) -> str:
    return hashlib.sha256(f"{prefix}:{value}".encode()).hexdigest()[:48]


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _nested_payload(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Return structured payloads worth inspecting, including JSON chunks."""
    payloads: list[dict[str, Any]] = [raw]
    data = raw.get("data")
    if isinstance(data, dict):
        payloads.append(data)
    chunk = raw.get("chunk")
    if isinstance(chunk, str) and chunk.lstrip().startswith("{"):
        try:
            parsed = json.loads(chunk)
            if isinstance(parsed, dict):
                payloads.append(parsed)
                if isinstance(parsed.get("data"), dict):
                    payloads.append(parsed["data"])
        except json.JSONDecodeError:
            pass
    return payloads


def extract_link_facts(raw: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Collect explicit native ids and transcript paths from one host record.

    The top-level host ``sessionId`` is deliberately skipped: it identifies the
    host run, not the underlying agent session.
    """
    ids: set[str] = set()
    paths: set[str] = set()

    def walk(value: Any, *, key_path: tuple[str, ...]) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).replace("-", "_").lower()
                at_top = not key_path
                if normalized in _NATIVE_ID_KEYS and isinstance(item, (str, int)):
                    candidate = str(item).strip()
                    if candidate and not (at_top and normalized == "sessionid"):
                        ids.add(candidate)
                if (
                    normalized in _TRANSCRIPT_KEYS
                    and isinstance(item, str)
                    and item.strip()
                ):
                    paths.add(item.strip())
                walk(item, key_path=(*key_path, normalized))
        elif isinstance(value, list):
            for item in value:
                walk(item, key_path=key_path)

    for payload in _nested_payload(raw):
        walk(payload, key_path=())
    return ids, paths


class StepGoHostSource:
    kind = "stepgo_host_jsonl"
    agent = "stepgo"
    parser_version = "2"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser()

    @staticmethod
    def identity(path: Path) -> str:
        digest = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
        return f"stepgo:host:{digest}"

    def discover(self) -> list[SourceDescriptor]:
        if not self.root.exists():
            return []
        paths = sorted(
            (path for path in self.root.glob("*.jsonl") if path.is_file()),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        return [
            SourceDescriptor(
                kind=self.kind,
                identity=self.identity(path),
                path=path,
                format="jsonl",
                agent=self.agent,
            )
            for path in paths
        ]

    def plan(self, source: SourceDescriptor, checkpoint: Any | None) -> SourcePlan:
        return self.plan_for_run(source, None)

    def plan_for_run(
        self, source: SourceDescriptor, run: StepGoRun | None
    ) -> SourcePlan:
        stat = source.path.stat()
        fingerprint = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "path": str(source.path.resolve()),
        }
        if run is None or run.parser_version != self.parser_version:
            return SourcePlan(source, PlanAction.REBUILD, file_fingerprint(source.path))
        if (
            run.index_status == "indexed"
            and run.file_size == stat.st_size
            and run.file_mtime_ns == stat.st_mtime_ns
        ):
            return SourcePlan(source, PlanAction.UNCHANGED, fingerprint)
        fingerprint = file_fingerprint(source.path)
        if (
            stat.st_size < run.last_indexed_offset
            or stat.st_size == run.file_size
            or not indexed_content_matches(source.path, run.fingerprint_json or {})
        ):
            return SourcePlan(source, PlanAction.REBUILD, fingerprint)
        return SourcePlan(source, PlanAction.APPEND, fingerprint)

    async def ingest(self, db: AsyncSession, plan: SourcePlan) -> StepGoRun:
        source = plan.source
        real_path = str(source.path.resolve())
        run = await db.scalar(
            select(StepGoRun).where(StepGoRun.source_identity == source.identity)
        )
        if run is None:
            run = StepGoRun(
                id=_id("stepgo-run", source.identity),
                host_run_id=source.path.stem,
                source_identity=source.identity,
                source_path=real_path,
                status="unknown",
                link_status="unlinked",
                raw_start_json={},
                raw_end_json={},
                metrics_json={},
            )
            db.add(run)
            await db.flush()
        run.source_path = real_path
        if plan.action == PlanAction.UNCHANGED:
            return run
        fingerprint = file_fingerprint(source.path)
        if plan.action == PlanAction.REBUILD:
            await db.execute(
                delete(StepGoRunEvent).where(StepGoRunEvent.run_id == run.id)
            )
            run.last_indexed_offset = 0
            run.last_indexed_line = 0
            run.event_count = 0
            run.agent = "unknown"
            run.module = None
            run.status = "unknown"
            run.link_status = "unlinked"
            run.link_reason = None
            run.linked_agent_session_id = None
            run.native_session_id = None
            run.transcript_path = None
            run.start_time = None
            run.end_time = None
            run.exit_code = None
            run.raw_start_json = {}
            run.raw_end_json = {}
            run.metrics_json = {}
            run.file_size = 0
            run.file_mtime_ns = 0
            run.file_sha256 = None
            run.last_error = None
        run.parser_version = self.parser_version

        last_event_index = await db.scalar(
            select(func.max(StepGoRunEvent.event_index)).where(
                StepGoRunEvent.run_id == run.id
            )
        )
        event_index = (
            last_event_index if last_event_index is not None else -1
        ) + 1
        run.index_status = "indexing"
        run.last_error = None
        # Appends after a terminal event must not flip a completed/failed run
        # back to "running" just because the new tail has no session.end.
        saw_end = bool(run.raw_end_json)
        ids: set[str] = set()
        paths: set[str] = set()
        if run.native_session_id:
            ids.add(run.native_session_id)
        if run.transcript_path:
            paths.add(run.transcript_path)
        pending_bytes = 0

        for record in iter_records(
            source.path,
            offset=_as_int(run.last_indexed_offset),
            line_offset=_as_int(run.last_indexed_line),
        ):
            raw = record.raw_json
            event_type = record.record_type
            event = StepGoRunEvent(
                run_id=run.id,
                event_index=event_index,
                line_number=record.line_number,
                byte_offset=record.byte_offset,
                event_type=event_type,
                event_timestamp=record.event_timestamp,
                raw_json=raw,
                raw_hash=record.raw_hash,
                is_known=event_type in _KNOWN_HOST_EVENTS,
            )
            db.add(event)
            pending_bytes += record.byte_length
            event_index += 1
            run.event_count += 1
            run.last_indexed_line = record.line_number
            run.last_indexed_offset = record.byte_offset + record.byte_length
            ts = record.event_timestamp
            if ts is not None:
                run.start_time = run.start_time or ts
            if event_type == "session.start":
                run.start_time = ts or run.start_time
                run.raw_start_json = raw
                if isinstance(raw.get("agent"), str):
                    run.agent = raw["agent"][:32]
                if isinstance(raw.get("module"), str):
                    run.module = raw["module"][:255]
                run.status = "running"
            elif event_type == "session.end":
                saw_end = True
                run.end_time = ts or run.end_time
                run.raw_end_json = raw
                data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
                if data.get("exitCode") is not None:
                    run.exit_code = _as_int(data.get("exitCode"))
                elif data.get("exit_code") is not None:
                    run.exit_code = _as_int(data.get("exit_code"))
                if isinstance(data.get("metrics"), dict):
                    run.metrics_json = data["metrics"]
                if run.exit_code not in (None, 0):
                    run.status = "failed"
                else:
                    run.status = "completed"
            elif event_type == "parse_error":
                run.index_status = "parse_error"
                run.last_error = str(raw.get("error") or "parse_error")[:500]
            record_ids, record_paths = extract_link_facts(raw)
            ids.update(record_ids)
            paths.update(record_paths)
            if event_index % 250 == 0 or pending_bytes >= 4 * 1024 * 1024:
                await db.flush()
                pending_bytes = 0

        if ids:
            run.native_session_id = next(iter(ids))[:255] if len(ids) == 1 else None
        if paths:
            run.transcript_path = next(iter(paths)) if len(paths) == 1 else None
        conflicting_facts = len(ids) > 1 or len(paths) > 1
        run.file_size = fingerprint["size"]
        run.file_mtime_ns = fingerprint["mtime_ns"]
        run.fingerprint_json = fingerprint
        run.file_sha256 = None
        if run.index_status != "parse_error":
            run.index_status = "indexed"
        if not saw_end:
            run.status = "unknown"
            run.end_time = None
        await db.flush()
        if conflicting_facts:
            run.link_status = "ambiguous"
            run.link_reason = "conflicting_native_identity"
            run.linked_agent_session_id = None
        else:
            await self.relink(db, run)
        return run

    async def relink(self, db: AsyncSession, run: StepGoRun) -> None:
        if run.link_reason == "conflicting_native_identity":
            return
        matches: dict[str, AgentSession] = {}
        ambiguous_paths: set[str] = set()
        ambiguous_ids: set[str] = set()

        if run.transcript_path:
            path = Path(run.transcript_path).expanduser()
            candidate = str(path.resolve(strict=False))
            rows = list(
                (
                    await db.scalars(
                        select(AgentSession).where(
                            or_(
                                AgentSession.source_path == candidate,
                                AgentSession.rollout_real_path == candidate,
                                AgentSession.rollout_path == candidate,
                            )
                        )
                    )
                ).all()
            )
            if len(rows) == 1:
                matches[rows[0].id] = rows[0]
            elif len(rows) > 1:
                ambiguous_paths.add(candidate)

        if run.native_session_id:
            rows = list(
                (
                    await db.scalars(
                        select(AgentSession).where(
                            AgentSession.thread_id == run.native_session_id,
                            AgentSession.agent == run.agent,
                        )
                    )
                ).all()
            )
            if len(rows) == 1:
                matches[rows[0].id] = rows[0]
            elif len(rows) > 1:
                ambiguous_ids.add(run.native_session_id)

        if len(matches) == 1 and not ambiguous_paths and not ambiguous_ids:
            run.linked_agent_session_id = next(iter(matches))
            run.link_status = "linked"
            run.link_reason = (
                "transcript_path" if run.transcript_path else "native_session_id"
            )
        elif len(matches) > 1 or ambiguous_paths or ambiguous_ids:
            run.linked_agent_session_id = None
            run.link_status = "ambiguous"
            run.link_reason = "multiple_native_sessions"
        else:
            run.linked_agent_session_id = None
            run.link_status = "unlinked"
            run.link_reason = (
                "native_session_not_indexed"
                if run.native_session_id or run.transcript_path
                else "no_native_identity"
            )
        run.updated_at = datetime.now(UTC)
        await db.flush()

    async def relink_pending(self, db: AsyncSession, *, limit: int = 200) -> int:
        """Retry runs whose native session may have been indexed later."""
        runs = list(
            (
                await db.scalars(
                    select(StepGoRun)
                    .where(
                        StepGoRun.link_status != "linked",
                        StepGoRun.link_reason.is_distinct_from("conflicting_native_identity"),
                        or_(
                            StepGoRun.native_session_id.is_not(None),
                            StepGoRun.transcript_path.is_not(None),
                        ),
                    )
                    .order_by(StepGoRun.updated_at)
                    .limit(limit)
                )
            ).all()
        )
        for run in runs:
            await self.relink(db, run)
        return len(runs)


__all__ = ["StepGoHostSource", "extract_link_facts"]
