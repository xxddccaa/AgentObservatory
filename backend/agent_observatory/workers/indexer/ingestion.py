"""Source ingestion and structured projection."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_observatory.db.models import AgentEvent, AgentSession, AgentTurn
from agent_observatory.services.history import project_events

from .adapters import PlanAction, SourcePlan
from .scanner import RolloutScanner
from .stepcode import StepCodeImporter


def _pg_safe(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "\\u0000")
    if isinstance(value, dict):
        return {str(_pg_safe(k)): _pg_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_pg_safe(v) for v in value]
    return value


class EventIngestor:
    def __init__(self, scanner: RolloutScanner, stepcode: StepCodeImporter) -> None:
        self.scanner = scanner
        self.stepcode = stepcode

    async def ingest_codex(self, db: AsyncSession, plan: SourcePlan) -> AgentSession:
        session = await self.scanner.index_path(
            db, plan.source.path, force_rebuild=plan.action == PlanAction.REBUILD
        )
        session.agent = plan.source.agent
        return session

    async def ingest_stepcode(
        self, db: AsyncSession, plan: SourcePlan, *, after_ordinal: int = -1
    ) -> AgentSession:
        source = plan.source
        assert source.thread_id is not None
        session = await db.scalar(
            select(AgentSession).where(
                AgentSession.source_identity == source.identity
            )
        )
        if session is None:
            session = AgentSession(
                id=hashlib.sha256(source.identity.encode()).hexdigest()[:48],
                thread_id=source.thread_id,
                rollout_path=str(source.path),
                rollout_real_path=source.identity,
                rollout_format="stepcode",
                source_kind="stepcode_sqlite",
                source_identity=source.identity,
                source_path=str(source.path),
                source_format="sqlite",
                parser_version=self.scanner.parser_version,
            )
            db.add(session)
            await db.flush()
        else:
            session.rollout_path = str(source.path)
            session.source_path = str(source.path)
        session.agent = source.agent
        if plan.action == PlanAction.REBUILD:
            await self.scanner._reset_session_index(db, session)
            after_ordinal = -1
        last_event_index = await db.scalar(
            select(AgentEvent.event_index)
            .where(AgentEvent.agent_session_id == session.id)
            .order_by(AgentEvent.event_index.desc())
            .limit(1)
        )
        event_index = int(last_event_index if last_event_index is not None else -1) + 1
        first_user = session.display_title
        first_event_ts = session.start_time
        source_cwd = session.cwd
        source_model = session.model
        batch: list[AgentEvent] = []
        for item in self.stepcode.iter_items(
            source.path,
            thread_id=source.thread_id,
            after_ordinal=after_ordinal,
            batch_size=500,
        ):
            raw = (
                item.payload
                if isinstance(item.payload, (dict, list))
                else {"type": item.item_type, "raw": item.raw_json}
            )
            if isinstance(raw, dict):
                source_cwd = source_cwd or raw.get("cwd") or raw.get("workingDirectory")
                source_model = source_model or raw.get("model") or raw.get("modelName")
            if (
                first_user is None
                and item.item_type.lower() in {"usermessage", "user_message"}
                and item.text
            ):
                first_user = item.text
            timestamp = (
                datetime.fromtimestamp(item.created_at_ms / 1000, tz=UTC)
                if item.created_at_ms
                else None
            )
            first_event_ts = first_event_ts or timestamp
            batch.append(
                AgentEvent(
                    agent_session_id=session.id,
                    event_index=event_index,
                    line_number=item.rollout_ordinal,
                    byte_offset=0,
                    event_timestamp=timestamp,
                    top_level_type=item.item_type,
                    payload_type=item.item_type,
                    normalized_type=item.item_type,
                    raw_json=_pg_safe(raw),
                    raw_hash=hashlib.sha256(item.raw_json.encode()).hexdigest(),
                    is_known=item.item_type.lower()
                    in {"usermessage", "assistantmessage", "reasoning", "commandexecution"},
                    parser_version=self.scanner.parser_version,
                )
            )
            event_index += 1
            if len(batch) >= 250:
                db.add_all(batch)
                await db.flush()
                batch.clear()
        if batch:
            db.add_all(batch)
        session.display_title = (first_user or source.thread_id)[:255]
        session.ai_title = session.ai_title or session.display_title
        if isinstance(source_cwd, str) and source_cwd:
            session.cwd = source_cwd
        if isinstance(source_model, str) and source_model:
            session.model = source_model
        stat = source.path.stat()
        state = self.stepcode.thread_state(source.path, source.thread_id)
        session.file_size = stat.st_size
        session.file_mtime_ns = stat.st_mtime_ns
        session.last_indexed_line = state["item_count"]
        session.index_status = "indexed"
        session.start_time = first_event_ts
        await db.flush()
        await project_events(db, session)
        # StepCode's native turn table is more authoritative than a projection
        # inferred from item shapes. Rebuild only this bounded relation.
        await db.execute(delete(AgentTurn).where(AgentTurn.agent_session_id == session.id))
        turn_count = 0
        latest_completed = session.end_time
        has_open_turn = False
        for turn in self.stepcode.iter_turns(
            source.path, thread_id=source.thread_id, batch_size=500
        ):
            turn_count += 1
            has_open_turn = has_open_turn or turn.completed_at is None
            completed_at = (
                datetime.fromtimestamp(turn.completed_at / 1000, tz=UTC)
                if turn.completed_at
                else None
            )
            if completed_at is not None and (
                latest_completed is None or completed_at > latest_completed
            ):
                latest_completed = completed_at
            db.add(
                AgentTurn(
                    id=hashlib.sha256(f"{session.id}:{turn.turn_id}".encode()).hexdigest()[:48],
                    agent=session.agent,
                    agent_session_id=session.id,
                    turn_index=turn.rollout_ordinal,
                    turn_id=turn.turn_id,
                    status=turn.status or "unknown",
                    started_at=(
                        datetime.fromtimestamp(turn.started_at / 1000, tz=UTC)
                        if turn.started_at
                        else None
                    ),
                    completed_at=completed_at,
                    duration_ms=turn.duration_ms,
                    outcome=(
                        "running"
                        if not turn.completed_at
                        else "completed"
                        if turn.status.lower() in {"completed", "complete", "success"}
                        else "failed"
                        if turn.status.lower() in {"failed", "error"}
                        else "interrupted"
                        if turn.status.lower() in {"interrupted", "aborted", "cancelled"}
                        else turn.status.lower() or "unknown"
                    ),
                    metric_confidence=(
                        "exact" if turn.duration_ms is not None else "derived"
                    ),
                    error_message=turn.error_json,
                )
            )
        session.turn_count = turn_count
        session.end_time = latest_completed
        session.is_ongoing = has_open_turn
        session.last_activity_at = (
            latest_completed
            or session.last_activity_at
            or session.start_time
        )
        await db.flush()
        return session


__all__ = ["EventIngestor"]
