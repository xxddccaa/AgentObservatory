from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import and_, exists, func, or_, select

from agent_observatory.api.deps import DB, CurrentPrincipal
from agent_observatory.db.models import (
    AgentEvent,
    AgentSession,
    AgentTurn,
    CodexMessage,
    StepGoRun,
    StepGoRunEvent,
    ToolCall,
    Workspace,
)
from agent_observatory.services.history import (
    ordered_transcript_items,
    render_transcript_json,
    render_transcript_markdown,
    render_transcript_text,
    search_session,
    serialize_event,
)
from agent_observatory.services.redaction import redact_value

router = APIRouter(prefix="/api/codex", tags=["codex-history"])

_EXPORT_EXT = {"text": "txt", "markdown": "md", "json": "json"}


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _activity_value(row: AgentSession) -> datetime | None:
    """Return source-derived activity only; never treat index time as activity."""
    return row.last_activity_at or row.end_time or row.start_time


def _stepgo_linked():
    return exists().where(StepGoRun.linked_agent_session_id == AgentSession.id)


def _stepgo_run_payload(run: StepGoRun, session: AgentSession | None = None) -> dict[str, Any]:
    return {
        "id": run.id,
        "host_run_id": run.host_run_id,
        "source_path": run.source_path,
        "agent": run.agent,
        "module": run.module,
        "status": run.status,
        "link_status": run.link_status,
        "link_reason": run.link_reason,
        "linked_agent_session_id": run.linked_agent_session_id,
        "native_session_id": run.native_session_id,
        "transcript_path": run.transcript_path,
        "start_time": _iso(run.start_time),
        "end_time": _iso(run.end_time),
        "exit_code": run.exit_code,
        "index_status": run.index_status,
        "event_count": run.event_count,
        "last_error": run.last_error,
        "session": (
            {
                "id": session.id,
                "thread_id": session.thread_id,
                "display_title": session.display_title,
                "ai_title": session.ai_title,
            }
            if session is not None
            else None
        ),
    }


def _encode_cursor(row: AgentSession) -> str:
    cursor_time = _activity_value(row) or row.updated_at
    raw = json.dumps([cursor_time.isoformat(), row.id]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(value: str) -> tuple[datetime, str]:
    try:
        padded = value + "=" * (-len(value) % 4)
        timestamp, session_id = json.loads(
            base64.urlsafe_b64decode(padded.encode()).decode()
        )
        parsed = datetime.fromisoformat(timestamp)
        parsed = (
            parsed.replace(tzinfo=UTC)
            if parsed.tzinfo is None
            else parsed.astimezone(UTC)
        )
        return parsed, str(session_id)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid cursor") from exc


@router.get("/sessions")
async def list_history(
    db: DB,
    principal: CurrentPrincipal,
    limit: int = Query(100, ge=1, le=200),
    cursor: str | None = None,
    q: str | None = None,
    is_ongoing: bool | None = None,
    agent: str | None = Query(None, min_length=1, max_length=32),
    workspace_id: str | None = None,
    include_subagents: bool = Query(
        False,
        description="Include child sessions spawned by another session; hidden by default.",
    ),
):
    stmt = select(AgentSession)
    if not include_subagents:
        stmt = stmt.where(AgentSession.is_subagent.is_(False))
    if workspace_id:
        stmt = stmt.where(AgentSession.workspace_id == workspace_id)
    if cursor:
        cursor_time, cursor_id = _decode_cursor(cursor)
        activity_expr = func.coalesce(
            AgentSession.last_activity_at,
            AgentSession.end_time,
            AgentSession.start_time,
            AgentSession.updated_at,
        )
        stmt = stmt.where(
            or_(
                activity_expr < cursor_time,
                and_(
                    activity_expr == cursor_time,
                    AgentSession.id < cursor_id,
                ),
            )
        )
    query = (q or "").strip()
    if query:
        pattern = f"%{query.replace('%', r'\%').replace('_', r'\_')}%"
        stmt = stmt.where(
            or_(
                AgentSession.thread_id.ilike(pattern, escape="\\"),
                AgentSession.cwd.ilike(pattern, escape="\\"),
                AgentSession.ai_title.ilike(pattern, escape="\\"),
                AgentSession.display_title.ilike(pattern, escape="\\"),
                AgentSession.source_kind.ilike(pattern, escape="\\"),
            )
        )
    if is_ongoing is not None:
        stmt = stmt.where(AgentSession.is_ongoing.is_(is_ongoing))
    if agent == "stepgo":
        stmt = stmt.where(_stepgo_linked())
    elif agent:
        stmt = stmt.where(AgentSession.agent == agent)
    activity_expr = func.coalesce(
        AgentSession.last_activity_at,
        AgentSession.end_time,
        AgentSession.start_time,
        AgentSession.updated_at,
    )
    rows = list(
        (
            await db.scalars(
                stmt.order_by(activity_expr.desc(), AgentSession.id.desc()).limit(limit + 1)
            )
        ).all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    workspace_ids = {row.workspace_id for row in rows if row.workspace_id}
    workspace_rows = (
        (
            await db.scalars(
                select(Workspace).where(Workspace.id.in_(workspace_ids))
            )
        ).all()
        if workspace_ids
        else []
    )
    workspaces = {row.id: row for row in workspace_rows}
    session_ids = [row.id for row in rows]
    run_rows = (
        list(
            (
                await db.scalars(
                    select(StepGoRun).where(
                        StepGoRun.linked_agent_session_id.in_(session_ids)
                    )
                )
            ).all()
        )
        if session_ids
        else []
    )
    runs_by_session: dict[str, list[StepGoRun]] = {}
    for run in run_rows:
        if run.linked_agent_session_id is not None:
            runs_by_session.setdefault(run.linked_agent_session_id, []).append(run)
    return {
        "sessions": [
            {
                "id": row.id,
                "thread_id": row.thread_id,
                "rollout_path": row.rollout_path,
                "cwd": row.cwd,
                "workspace_id": row.workspace_id,
                "workspace_kind": (
                    workspaces[row.workspace_id].workspace_kind
                    if row.workspace_id in workspaces
                    else None
                ),
                "workspace_confidence": (
                    workspaces[row.workspace_id].workspace_confidence
                    if row.workspace_id in workspaces
                    else None
                ),
                "ai_title": row.ai_title,
                "display_title": row.display_title,
                "agent": row.agent,
                "is_subagent": row.is_subagent,
                "parent_session_id": row.parent_session_id,
                "source_kind": row.source_kind,
                "source_path": row.source_path,
                "model": row.model,
                "turn_count": row.turn_count,
                "total_tokens": row.total_tokens,
                "is_ongoing": row.is_ongoing,
                "updated_at": _iso(row.updated_at),
                "activity_at": _iso(_activity_value(row)),
                "index_status": row.index_status,
                "launch_sources": (
                    ["stepgo"] if row.id in runs_by_session else []
                ),
                "stepgo_run_count": len(runs_by_session.get(row.id, [])),
            }
            for row in rows
        ],
        "next_cursor": _encode_cursor(rows[-1]) if has_more and rows else None,
    }


async def _session(db, session_id: str) -> AgentSession:
    row = await db.get(AgentSession, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Agent session not found")
    return row


@router.get("/sessions/{session_id}")
async def get_history(session_id: str, db: DB, principal: CurrentPrincipal):
    row = await _session(db, session_id)
    runs = list(
        (
            await db.scalars(
                select(StepGoRun)
                .where(StepGoRun.linked_agent_session_id == session_id)
                .order_by(StepGoRun.start_time.desc(), StepGoRun.id)
            )
        ).all()
    )
    return {
        "id": row.id,
        "thread_id": row.thread_id,
        "rollout_path": row.rollout_path,
        "cwd": row.cwd,
        "activity_at": _iso(_activity_value(row)),
        "model": row.model,
        "turn_count": row.turn_count,
        "total_tokens": row.total_tokens,
        "display_title": row.display_title,
        "agent": row.agent,
        "is_subagent": row.is_subagent,
        "parent_session_id": row.parent_session_id,
        "source_kind": row.source_kind,
        "source_path": row.source_path,
        "is_ongoing": row.is_ongoing,
        "updated_at": _iso(row.updated_at),
        "launch_sources": ["stepgo"] if runs else [],
        "stepgo_runs": [_stepgo_run_payload(run) for run in runs],
    }


@router.get("/stepgo/runs")
async def list_stepgo_runs(
    db: DB,
    principal: CurrentPrincipal,
    limit: int = Query(100, ge=1, le=200),
    link_status: str | None = None,
    agent: str | None = Query(None, min_length=1, max_length=32),
    cursor: str | None = None,
):
    activity = func.coalesce(StepGoRun.start_time, StepGoRun.created_at)
    stmt = (
        select(StepGoRun, AgentSession)
        .outerjoin(
            AgentSession, AgentSession.id == StepGoRun.linked_agent_session_id
        )
        .order_by(activity.desc(), StepGoRun.id.desc())
        .limit(limit + 1)
    )
    if link_status:
        stmt = stmt.where(StepGoRun.link_status == link_status)
    if agent:
        stmt = stmt.where(StepGoRun.agent == agent)
    if cursor:
        cursor_time, cursor_id = _decode_cursor(cursor)
        stmt = stmt.where(or_(
            activity < cursor_time,
            and_(activity == cursor_time, StepGoRun.id < cursor_id),
        ))
    rows = (await db.execute(stmt)).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more:
        last_run = rows[-1][0]
        raw = json.dumps([(last_run.start_time or last_run.created_at).isoformat(), last_run.id]).encode()
        next_cursor = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return {
        "runs": [_stepgo_run_payload(run, session) for run, session in rows],
        "next_cursor": next_cursor,
    }


@router.get("/stepgo/runs/{run_id}/events")
async def list_stepgo_run_events(
    run_id: str,
    db: DB,
    principal: CurrentPrincipal,
    limit: int = Query(500, ge=1, le=2000),
    after: int = Query(-1, ge=-1),
):
    if await db.get(StepGoRun, run_id) is None:
        raise HTTPException(status_code=404, detail="StepGo run not found")
    rows = list(
        (
            await db.scalars(
                select(StepGoRunEvent)
                .where(StepGoRunEvent.run_id == run_id, StepGoRunEvent.event_index > after)
                .order_by(StepGoRunEvent.event_index)
                .limit(limit + 1)
            )
        ).all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "events": [
            {
                "event_index": row.event_index,
                "line_number": row.line_number,
                "byte_offset": row.byte_offset,
                "event_type": row.event_type,
                "event_timestamp": _iso(row.event_timestamp),
                "is_known": row.is_known,
                "raw_json": redact_value(row.raw_json),
            }
            for row in rows
        ],
        "truncated": has_more,
        "next_cursor": rows[-1].event_index if has_more else None,
    }


@router.get("/sessions/{session_id}/stepgo-runs")
async def session_stepgo_runs(session_id: str, db: DB, principal: CurrentPrincipal):
    await _session(db, session_id)
    rows = list(
        (
            await db.scalars(
                select(StepGoRun)
                .where(StepGoRun.linked_agent_session_id == session_id)
                .order_by(StepGoRun.start_time.desc(), StepGoRun.id)
            )
        ).all()
    )
    return {"runs": [_stepgo_run_payload(run) for run in rows]}


@router.get("/sessions/{session_id}/events")
async def history_events(session_id: str, db: DB, principal: CurrentPrincipal):
    await _session(db, session_id)
    rows = list(
        (
            await db.scalars(
                select(AgentEvent)
                .where(AgentEvent.agent_session_id == session_id)
                .order_by(AgentEvent.event_index)
            )
        ).all()
    )
    return {"events": [serialize_event(row) for row in rows]}


@router.get("/sessions/{session_id}/messages")
async def history_messages(session_id: str, db: DB, principal: CurrentPrincipal):
    await _session(db, session_id)
    rows = list(
        (
            await db.scalars(
                select(CodexMessage)
                .where(CodexMessage.agent_session_id == session_id)
                .order_by(CodexMessage.sequence)
            )
        ).all()
    )
    return {
        "messages": [
            {
                "id": row.id,
                "role": row.role,
                "channel": row.channel,
                "text": row.text,
                "markdown_text": row.markdown_text,
                "copy_text": row.copy_text,
                "sequence": row.sequence,
                "timestamp": _iso(row.timestamp),
                "turn_id": row.turn_id,
            }
            for row in rows
        ]
    }


@router.get("/sessions/{session_id}/turns")
async def history_turns(session_id: str, db: DB, principal: CurrentPrincipal):
    await _session(db, session_id)
    rows = list(
        (
            await db.scalars(
                select(AgentTurn)
                .where(AgentTurn.agent_session_id == session_id)
                .order_by(AgentTurn.turn_index)
            )
        ).all()
    )
    return {
        "turns": [
            {
                "id": row.id,
                "turn_index": row.turn_index,
                "turn_id": row.turn_id,
                "status": row.status,
                "total_tokens": row.total_tokens,
                "started_at": _iso(row.started_at),
                "completed_at": _iso(row.completed_at),
                "error_kind": row.error_kind,
            }
            for row in rows
        ]
    }


@router.get("/sessions/{session_id}/tools")
async def history_tools(session_id: str, db: DB, principal: CurrentPrincipal):
    await _session(db, session_id)
    rows = list(
        (
            await db.scalars(
                select(ToolCall)
                .where(ToolCall.agent_session_id == session_id)
                .order_by(ToolCall.started_at, ToolCall.created_at)
            )
        ).all()
    )
    return {
        "tools": [
            {
                "id": row.id,
                "call_id": row.call_id,
                "tool_name": row.tool_name,
                "tool_kind": row.tool_kind,
                "command_text": row.command_text,
                "arguments_json": row.arguments_json,
                "output_text": row.output_text,
                "exit_code": row.exit_code,
                "status": row.status,
                "is_mutating": row.is_mutating,
                "started_at": _iso(row.started_at),
            }
            for row in rows
        ]
    }


@router.get("/sessions/{session_id}/export")
async def export_session(
    session_id: str,
    db: DB,
    principal: CurrentPrincipal,
    format: str = Query("text"),
):
    session = await _session(db, session_id)
    if format not in _EXPORT_EXT:
        raise HTTPException(status_code=400, detail="format must be one of text|markdown|json")

    events = list(
        (
            await db.scalars(
                select(AgentEvent)
                .where(AgentEvent.agent_session_id == session_id)
                .order_by(AgentEvent.event_index)
            )
        ).all()
    )
    if format == "json":
        content = render_transcript_json(events)
    else:
        messages = list(
            (
                await db.scalars(
                    select(CodexMessage)
                    .where(CodexMessage.agent_session_id == session_id)
                    .order_by(CodexMessage.sequence)
                )
            ).all()
        )
        tools = list(
            (
                await db.scalars(
                    select(ToolCall).where(ToolCall.agent_session_id == session_id)
                )
            ).all()
        )
        items = ordered_transcript_items(events, messages, tools)
        content = (
            render_transcript_text(session, items)
            if format == "text"
            else render_transcript_markdown(session, items)
        )

    filename = f"{session.agent}-{session.thread_id}.{_EXPORT_EXT[format]}"
    return {"format": format, "filename": filename, "content": content}


@router.get("/sessions/{session_id}/search")
async def search_history(
    session_id: str,
    db: DB,
    principal: CurrentPrincipal,
    q: str = Query(""),
    limit: int = Query(100, ge=1, le=500),
):
    await _session(db, session_id)
    query = q.strip()
    if not query:
        return {"query": query, "matches": []}
    matches = await search_session(db, session_id, query, limit)
    return {"query": query, "matches": matches}
