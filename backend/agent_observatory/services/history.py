"""Projection of raw Codex rollout events into messages / tools / turns.

The indexer stores every rollout line verbatim as a :class:`AgentEvent` row.
This module turns those rows into the structured, UI-facing shapes
(:class:`CodexMessage`, :class:`ToolCall`, :class:`AgentTurn`) and renders
whole-session transcripts for the export endpoint.

The event taxonomy mirrors real Codex rollouts (v0.146-0.149):

* Turn boundaries -- ``event_msg`` payload ``task_started`` / ``task_complete``
  / ``turn_aborted`` (with ``turn_started`` / ``turn_completed`` as fallbacks).
* Messages -- ``event_msg`` ``user_message`` / ``agent_message`` /
  ``agent_reasoning`` plus ``response_item`` ``message`` / ``reasoning``.
* Tools -- ``response_item`` ``function_call`` + ``function_call_output`` and
  ``custom_tool_call`` + ``custom_tool_call_output`` (paired by ``call_id``),
  with ``event_msg`` ``patch_apply_end`` supplying apply_patch results.
* Tokens -- ``event_msg`` ``token_count`` (``info.total_token_usage`` is the
  cumulative session total; ``info.last_token_usage`` is per request).
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, exists, func, insert, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_observatory.db.models import (
    ActivitySpan,
    AgentEvent,
    AgentSession,
    AgentTurn,
    CodexMessage,
    EvidenceRecord,
    LocalDataSource,
    SessionSkillEvidence,
    SkillDefinition,
    ToolCall,
)
from agent_observatory.services.redaction import redact_secrets, redact_value

#: A session is considered ongoing only if its last turn never completed *and*
#: the rollout file was written to within this many seconds.
ONGOING_MTIME_WINDOW_SECONDS = 60

_EXEC_TOOLS = {"exec_command", "shell", "local_shell", "container.exec"}
_STDIN_TOOLS = {"write_stdin", "send_input"}

_TOOL_KIND = {
    "exec_command": "exec",
    "shell": "exec",
    "local_shell": "exec",
    "container.exec": "exec",
    "write_stdin": "stdin",
    "send_input": "stdin",
    "apply_patch": "patch",
    "update_plan": "plan",
    "view_image": "image",
    "web_search_call": "web_search",
    "spawn_agent": "agent",
    "wait_agent": "agent",
    "close_agent": "agent",
    "get_goal": "goal",
    "create_goal": "goal",
    "update_goal": "goal",
    "request_user_input": "input",
    "wait": "wait",
}

#: Heuristic: shell fragments that write to the filesystem / repo state.
_MUTATING_CMD_RE = re.compile(
    r"(?:^|[|&;]\s*|\bsudo\s+)(?:rm|mv|cp|dd|tee|truncate|mkdir|rmdir|touch|chmod|"
    r"chown|ln|install|patch)\b"
    r"|>>?"
    r"|\bsed\s+-i"
    r"|\bgit\s+(?:add|commit|push|reset|checkout|rm|mv|apply|stash|clean)\b"
    r"|\b(?:npm|pnpm|yarn)\s+(?:install|i|ci|add)\b"
    r"|\bpip\s+install\b"
    r"|\bapt(?:-get)?\s+install\b"
    r"|\bcargo\s+(?:build|install)\b"
    r"|\bmake\b"
)
_EXIT_CODE_RE = re.compile(r"exited with code (-?\d+)")
_VALIDATION_CMD_RE = re.compile(
    r"(?:^|\s)(?:"
    r"pytest|python(?:3)?\s+-m\s+(?:pytest|py_compile)|"
    r"uv\s+run\s+(?:pytest|ruff|mypy|alembic|python\s+-m\s+py_compile)|"
    r"ruff\s+check|mypy|alembic\s+check|"
    r"npm\s+(?:run\s+)?(?:test|check|build)|"
    r"npx\s+(?:playwright|vitest)|"
    r"(?:vue-)?tsc|cargo\s+(?:test|check)|go\s+test|make\s+(?:test|check)"
    r")\b",
    re.IGNORECASE,
)

_AGENT_ITEM_TYPES = {
    "agentmessage",
    "reasoning",
    "commandexecution",
    "websearch",
    "filechange",
    "imageview",
    "collabagenttoolcall",
    "subagentactivity",
}

_ITEM_SPAN_TYPES = {
    "agentmessage": "agent_message",
    "reasoning": "reasoning",
    "commandexecution": "tool",
    "websearch": "web_search",
    "filechange": "file_change",
    "imageview": "image_view",
    "collabagenttoolcall": "agent_tool",
    "subagentactivity": "subagent",
    "contextcompaction": "context_compaction",
    "usermessage": "user_message",
    "plan": "plan",
}
_WORLD_SKILL_RE = re.compile(r"(?m)^\s*-\s+([A-Za-z0-9_.-]+):\s+")


def _id() -> str:
    return secrets.token_hex(12)


def _stable_id(namespace: str, value: str) -> str:
    return hashlib.sha256(f"{namespace}:{value}".encode()).hexdigest()[:48]


def _pg_safe(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "\\u0000")
    if isinstance(value, dict):
        return {str(_pg_safe(key)): _pg_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_pg_safe(item) for item in value]
    return value


def _text_value(value: Any) -> str:
    """Flatten a scalar / list-of-strings / structured value to plain text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return _pg_safe(value)
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
            else:
                parts.append(_text_value(item))
        return "\n".join(p for p in parts if p)
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return value["text"]
    return json.dumps(_pg_safe(value), ensure_ascii=False)


def _content_text(content: Any) -> str:
    """Join the ``text`` fields of a ``response_item.message`` content array."""
    if not isinstance(content, list):
        return _text_value(content)
    parts = [
        _pg_safe(item["text"])
        for item in content
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    ]
    return "\n".join(parts)


def _summary_text(summary: Any) -> str:
    """Join the ``text`` fields of a ``response_item.reasoning`` summary array."""
    if not isinstance(summary, list):
        return _text_value(summary)
    parts = [
        _pg_safe(item["text"])
        for item in summary
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    ]
    return "\n".join(parts)


def _load_json(value: str) -> Any:
    try:
        return _pg_safe(json.loads(value))
    except (ValueError, TypeError):
        return None


def _tool_kind(name: str) -> str:
    return _TOOL_KIND.get(name, "function")


def _bounded_label(value: Any, limit: int = 255) -> str:
    """Bound varchar labels while preserving full command text in Text fields."""
    return str(value)[:limit]


def _command_text(name: str, arguments: dict[str, Any] | None) -> str | None:
    if not isinstance(arguments, dict):
        return None
    if name in _EXEC_TOOLS:
        return _text_value(arguments.get("cmd") or arguments.get("command")) or None
    if name in _STDIN_TOOLS:
        return _text_value(arguments.get("chars") or arguments.get("message")) or None
    if name == "apply_patch":
        return _text_value(arguments.get("input") or arguments.get("patch")) or None
    return None


def _is_mutating(name: str, command_text: str | None) -> bool:
    if name == "apply_patch":
        return True
    if name in _EXEC_TOOLS and command_text:
        return bool(_MUTATING_CMD_RE.search(command_text))
    return False


def _parse_exit_code(output: str | None) -> int | None:
    if not output:
        return None
    match = _EXIT_CODE_RE.search(output)
    return int(match.group(1)) if match else None


def _looks_failed(output: str | None) -> bool:
    if not output:
        return False
    low = output.lstrip().lower()
    return "<tool_use_error>" in low or low.startswith("error:")


def _is_validation_command(command: str | None) -> bool:
    return bool(command and _VALIDATION_CMD_RE.search(command))


def _from_epoch_ms(value: Any) -> datetime | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _elapsed_ms(started_at: datetime | None, completed_at: datetime | None) -> int | None:
    if started_at is None or completed_at is None:
        return None
    return max(0, round((completed_at - started_at).total_seconds() * 1000))


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
        total += _elapsed_ms(start, end) or 0
        start, end = next_start, next_end
    return total + (_elapsed_ms(start, end) or 0)


def _item_command(item: dict[str, Any]) -> str | None:
    command = item.get("command")
    if isinstance(command, list):
        text = " ".join(str(part) for part in command if part is not None).strip()
        return text or None
    if isinstance(command, str):
        return command or None
    return None


def _item_status(item: dict[str, Any]) -> str:
    value = str(item.get("status") or "").lower()
    exit_code = item.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        return "failed"
    if value in {"failed", "error", "declined"}:
        return "failed"
    if value in {"completed", "success", "ok"} or exit_code == 0:
        return "ok"
    return value or "completed"


def _output_text(output: Any) -> str | None:
    if output is None:
        return None
    if isinstance(output, str):
        return _pg_safe(output)
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            if isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif item.get("type"):
                    parts.append(f"[{item['type']}]")
        return "\n".join(parts)
    return json.dumps(_pg_safe(output), ensure_ascii=False)


def _usage_snapshot(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    return {
        "input_tokens": int(value.get("input_tokens") or 0),
        "cached_input_tokens": int(value.get("cached_input_tokens") or 0),
        "cache_creation_input_tokens": int(
            value.get("cache_write_input_tokens")
            or value.get("cache_creation_input_tokens")
            or 0
        ),
        "output_tokens": int(value.get("output_tokens") or 0),
        "reasoning_output_tokens": int(value.get("reasoning_output_tokens") or 0),
        "total_tokens": int(
            value.get("total_tokens")
            or sum(
                int(value.get(key) or 0)
                for key in (
                    "input_tokens",
                    "output_tokens",
                )
            )
        ),
    }


def _message_projection(top: str | None, ptype: str | None, payload: dict[str, Any]) -> tuple[str, str, str] | None:
    """Return ``(role, channel, text)`` for an event that projects to a message.

    User turns are taken from ``event_msg/user_message`` (the clean, authoritative
    user input); ``response_item/message`` with role user/developer/system is
    injected scaffolding (AGENTS.md, permissions, interrupt markers) or a
    duplicate of the user_message, so it is skipped. Assistant replies and
    reasoning are accepted from either source and de-duplicated by the caller.
    """
    if str(top or "").lower() in {"usermessage", "user_message"}:
        return ("user", "final", _content_text(payload.get("content")))
    if str(top or "").lower() in {"assistantmessage", "agentmessage", "assistant_message", "agent_message"}:
        return ("assistant", "final", _content_text(payload.get("content")))
    if str(top or "").lower() in {"reasoning", "reasoningitem"}:
        return ("assistant", "reasoning", _text_value(payload.get("summary_text") or payload.get("summary") or payload.get("content")))
    if top == "event_msg":
        if ptype == "user_message":
            return ("user", "final", _text_value(payload.get("message")))
        if ptype == "agent_message":
            return ("assistant", "final", _text_value(payload.get("message")))
        if ptype == "agent_reasoning":
            return ("assistant", "reasoning", _text_value(payload.get("text")))
        if ptype == "item_completed" and isinstance(payload.get("item"), dict):
            item = payload["item"]
            item_type = str(item.get("type") or "").lower()
            if item_type == "usermessage":
                return ("user", "final", _content_text(item.get("content")))
            if item_type == "agentmessage":
                return ("assistant", "final", _content_text(item.get("content")))
            if item_type == "reasoning":
                return (
                    "assistant",
                    "reasoning",
                    _text_value(item.get("summary_text") or item.get("content")),
                )
        return None
    if top == "response_item":
        if ptype == "message":
            role = str(payload.get("role") or "assistant")
            if role != "assistant":
                return None
            return ("assistant", "final", _content_text(payload.get("content")))
        if ptype == "reasoning":
            return ("assistant", "reasoning", _summary_text(payload.get("summary")))
    if top == "message":
        role = str(payload.get("role") or "assistant")
        if role in {"user", "assistant"}:
            return (role, "final", _content_text(payload.get("content")))
    if top == "reasoning":
        return ("assistant", "reasoning", _summary_text(payload.get("summary")))
    return None


def _markdown_for(channel: str, text: str) -> str:
    """A reasonable Markdown rendering of message text.

    Assistant/user text is already Markdown-ish, so it is preserved verbatim.
    Reasoning summaries are rendered under an emphasised label so a viewer can
    visually separate them from final answers without corrupting embedded code
    fences.
    """
    if channel == "reasoning":
        return f"*Reasoning*\n\n{text}" if text else ""
    return text


def _apply_session_status(
    session: AgentSession, turns: list[AgentTurn], last_event_ts: datetime | None
) -> None:
    """Derive ``is_ongoing`` / ``end_time`` from the projected turns + file mtime."""
    now = datetime.now(UTC)
    mtime: datetime | None = None
    if session.file_mtime_ns:
        mtime = datetime.fromtimestamp(session.file_mtime_ns / 1_000_000_000, tz=UTC)
    last_turn = turns[-1] if turns else None
    open_turn = (
        last_turn is not None
        and last_turn.completed_at is None
        and last_turn.status in ("running", "unknown")
    )
    recent = mtime is not None and (now - mtime).total_seconds() <= ONGOING_MTIME_WINDOW_SECONDS
    session.is_ongoing = bool(open_turn and recent)
    session.last_activity_at = last_event_ts or (
        last_turn.completed_at if last_turn is not None else None
    ) or session.start_time
    if session.is_ongoing:
        session.end_time = None
    elif last_turn is not None and last_turn.completed_at is not None:
        session.end_time = last_turn.completed_at
    else:
        session.end_time = last_event_ts


async def project_events(db: AsyncSession, session: AgentSession) -> bool:
    """(Re)build messages, tool calls and turns for ``session`` from its events.

    The projection is idempotent: it deletes any previously projected rows for
    the session and rebuilds them from the stored :class:`AgentEvent` rows, so
    re-indexing a session refreshes its structured view. Session-level
    aggregates (``turn_count``, token totals, ``is_ongoing`` / ``end_time``) are
    recomputed here as the single source of truth.

    Returns ``True`` when rows were rebuilt, ``False`` when the fast path found
    the projection already current.

    A no-op fast path skips the rebuild when the session's event set is
    unchanged (``projected_event_index`` already covers the newest event).
    Events are append-only per session: the scanner resets
    ``projected_event_index`` to ``-1`` whenever it rebuilds a session's event
    rows, so a stale projection can never be mistaken for a current one.

    Evidence rows are written in batches; a rebuild of a few thousand events
    used to spend most of its time in one round trip per evidence row.
    """
    max_event_index = await db.scalar(
        select(func.max(AgentEvent.event_index)).where(
            AgentEvent.agent_session_id == session.id
        )
    )
    max_event_index = int(max_event_index) if max_event_index is not None else -1
    if session.projected_event_index == max_event_index and max_event_index >= 0:
        # The structured rows are current, but ``is_ongoing`` / ``end_time``
        # also depend on the file mtime and on wall-clock time, and a metadata
        # -only change can move both.  Refresh those two fields from the last
        # turn instead of leaving them stale until the next real event.
        last_turn = await db.scalar(
            select(AgentTurn)
            .where(AgentTurn.agent_session_id == session.id)
            .order_by(AgentTurn.turn_index.desc())
            .limit(1)
        )
        last_event_ts = await db.scalar(
            select(func.max(AgentEvent.event_timestamp)).where(
                AgentEvent.agent_session_id == session.id
            )
        )
        _apply_session_status(
            session, [last_turn] if last_turn is not None else [], last_event_ts
        )
        await db.flush()
        return False

    rollout_source_id = f"source-rollout-events-{session.agent}"[:64]
    rollout_source = await db.get(LocalDataSource, rollout_source_id)
    if rollout_source is None:
        rollout_source = LocalDataSource(
            id=rollout_source_id,
            agent=session.agent,
            source_kind=f"{session.source_kind}_evidence",
            source_identity=f"{session.agent}:rollout-evidence",
            source_path=session.source_path or session.rollout_path or "",
            source_format=session.source_format,
            parser_version=session.parser_version,
            status="indexed",
        )
        db.add(rollout_source)
        await db.flush()
    else:
        rollout_source.agent = session.agent
        rollout_source.status = "indexed"
        rollout_source.parser_version = session.parser_version
        rollout_source.last_success_at = datetime.now(UTC)

    skill_ids = dict(
        (
            await db.execute(select(SkillDefinition.name, SkillDefinition.id))
        ).all()
    )

    has_xdcode_usage_records = bool(
        session.agent == "xdcode"
        and await db.scalar(
            select(
                exists().where(
                    AgentEvent.agent_session_id == session.id,
                    AgentEvent.top_level_type == "token_usage_record",
                )
            )
        )
    )

    event_stream = await db.stream(
        select(
            AgentEvent.id,
            AgentEvent.event_index,
            AgentEvent.line_number,
            AgentEvent.raw_json,
            AgentEvent.raw_hash,
            AgentEvent.top_level_type,
            AgentEvent.payload_type,
            AgentEvent.event_timestamp,
        )
        .where(AgentEvent.agent_session_id == session.id)
        .order_by(AgentEvent.event_index)
        .execution_options(yield_per=250)
    )

    # Idempotent rebuild: local-source spans are separately owned and survive
    # rollout reprojection. Rollout evidence is rebuilt against current event IDs.
    await db.execute(
        delete(ActivitySpan).where(
            ActivitySpan.agent_session_id == session.id,
            ActivitySpan.source_kind == "rollout",
        )
    )
    await db.execute(
        delete(EvidenceRecord).where(
            EvidenceRecord.agent_session_id == session.id,
            EvidenceRecord.source_id == rollout_source.id,
        )
    )
    await db.execute(
        delete(SessionSkillEvidence).where(
            SessionSkillEvidence.agent_session_id == session.id,
            SessionSkillEvidence.source_record_id.like("rollout:%"),
        )
    )
    # Messages and tools reference turns, so remove them before turn rows.
    for model in (CodexMessage, ToolCall, AgentTurn):
        await db.execute(
            delete(model)
            .where(model.agent_session_id == session.id)
            .execution_options(synchronize_session=False)
        )
    await db.flush()

    turns: list[AgentTurn] = []
    turns_by_native_id: dict[str, AgentTurn] = {}
    seen_usage_responses: set[tuple[str, str]] = set()
    current_turn: AgentTurn | None = None
    turn_index = -1
    tools_by_call: dict[str, ToolCall] = {}
    sequence = 0
    seen_messages: set[tuple[str | None, str, str, str]] = set()
    last_event_ts: datetime | None = None
    session_totals: dict[str, Any] | None = None
    pending_outputs: dict[str, tuple[str | None, int | None, datetime | None]] = {}
    validation_results: dict[str, list[bool]] = {}
    mutating_turns: set[str] = set()
    tool_intervals: dict[str, list[tuple[datetime, datetime]]] = {}
    exact_tool_commands: set[tuple[str, str]] = set()
    tool_evidence_ids: dict[str, str] = {}
    turn_evidence_ids: dict[str, str] = {}

    evidence_batch: list[dict[str, Any]] = []
    evidence_batch_size = 250

    async def flush_evidence() -> None:
        """Insert buffered evidence rows in one round trip.

        Evidence is referenced by primary key from activity spans and skill
        evidence, so callers must flush before adding a row that points at a
        buffered evidence id.
        """
        if not evidence_batch:
            return
        rows = list(evidence_batch)
        evidence_batch.clear()
        # Autoflush must stay off here: a pending span/skill row may reference
        # an id in this batch, and the ORM flush would insert that row first.
        with db.no_autoflush:
            await db.execute(insert(EvidenceRecord), rows)

    async def add_rollout_evidence(
        event: Any,
        *,
        category: str,
        summary: str | None = None,
        turn_id: str | None = None,
    ) -> str:
        evidence_id = _stable_id(
            "rollout-evidence", f"{session.id}:{event.event_index}"
        )
        evidence_batch.append(
            {
                "id": evidence_id,
                "source_id": rollout_source.id,
                "source_record_id": f"{session.id}:{event.event_index}",
                "agent_session_id": session.id,
                "agent_event_id": event.id,
                "thread_id": session.thread_id,
                "turn_id": turn_id,
                "recorded_at": event.event_timestamp,
                "category": category,
                "summary": summary,
                "raw_hash": event.raw_hash,
                "source_locator": {
                    "event_index": event.event_index,
                    "line_number": event.line_number,
                },
                "metadata_json": {"top_level_type": event.top_level_type},
            }
        )
        if len(evidence_batch) >= evidence_batch_size:
            await flush_evidence()
        return evidence_id

    events_since_flush = 0

    async for event in event_stream:
        # Bound both memory and round trips: the unit of work orders inserts by
        # foreign-key dependency, so rows may safely wait for the next flush.
        events_since_flush += 1
        if events_since_flush >= 1000:
            events_since_flush = 0
            await flush_evidence()
            await db.flush()
        raw = event.raw_json
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
        if not isinstance(payload, dict):
            payload = {}
        top = event.top_level_type
        ptype = event.payload_type
        ts = event.event_timestamp
        if ts is not None:
            last_event_ts = ts

        if top == "world_state":
            state = payload.get("state")
            if isinstance(state, dict):
                available: dict[str, str] = {}
                for state_key in ("host_skills", "skills", "orchestrator_skills"):
                    value = state.get(state_key)
                    body = (
                        value
                        if isinstance(value, str)
                        else value.get("body")
                        if isinstance(value, dict)
                        else None
                    )
                    if not isinstance(body, str):
                        continue
                    for match in _WORLD_SKILL_RE.finditer(body):
                        available.setdefault(match.group(1), state_key)
                if available:
                    evidence_id = await add_rollout_evidence(
                        event,
                        category="skill_snapshot",
                        summary=f"{len(available)} available skills",
                    )
                    for skill_name, state_key in sorted(available.items()):
                        source_record_id = (
                            f"rollout:{event.event_index}:{state_key}:{skill_name}"
                        )
                        db.add(
                            SessionSkillEvidence(
                                id=_stable_id(
                                    "session-skill",
                                    f"{session.id}:{source_record_id}",
                                ),
                                agent_session_id=session.id,
                                skill_id=skill_ids.get(skill_name),
                                evidence_id=evidence_id,
                                turn_id=current_turn.turn_id if current_turn else None,
                                skill_name=skill_name,
                                evidence_type="available",
                                confidence="exact",
                                source_record_id=source_record_id,
                                event_time=ts,
                                metadata_json={"state_source": state_key},
                            )
                        )
            continue

        # --- turn boundaries -------------------------------------------------
        if ptype in ("task_started", "turn_started"):
            turn_index += 1
            current_turn = AgentTurn(
                id=_id(),
                agent=session.agent,
                agent_session_id=session.id,
                turn_index=turn_index,
                turn_id=(str(payload.get("turn_id")) if payload.get("turn_id") else None),
                status="running",
                started_at=ts,
                input_tokens=0,
                cached_input_tokens=0,
                output_tokens=0,
                reasoning_output_tokens=0,
                total_tokens=0,
                outcome="running",
            )
            db.add(current_turn)
            turns.append(current_turn)
            if current_turn.turn_id:
                turns_by_native_id[current_turn.turn_id] = current_turn
            turn_evidence_ids[current_turn.id] = await add_rollout_evidence(
                event,
                category="turn_started",
                summary=f"Turn {turn_index + 1} started",
                turn_id=current_turn.turn_id,
            )
            continue
        if ptype in ("task_complete", "turn_completed"):
            if current_turn is not None:
                current_turn.status = "completed"
                current_turn.outcome = "completed"
                current_turn.completed_at = ts or current_turn.completed_at
                current_turn.duration_ms = _elapsed_ms(
                    current_turn.started_at, current_turn.completed_at
                )
            continue
        if ptype in ("turn_aborted", "turn_failed"):
            if current_turn is not None:
                current_turn.status = "aborted" if ptype == "turn_aborted" else "failed"
                current_turn.outcome = current_turn.status
                current_turn.completed_at = ts or current_turn.completed_at
                current_turn.duration_ms = _elapsed_ms(
                    current_turn.started_at, current_turn.completed_at
                )
                current_turn.error_kind = (
                    str(payload.get("reason") or payload.get("error_kind") or ptype) or None
                )
                current_turn.error_message = (
                    str(payload["message"]) if payload.get("message") else payload.get("error")
                )
            continue
        if ptype == "error":
            if current_turn is not None:
                current_turn.status = "failed"
                current_turn.outcome = "failed"
                current_turn.error_kind = str(
                    payload.get("error_kind") or payload.get("kind") or "error"
                )
                current_turn.error_message = str(
                    payload.get("message") or payload.get("error") or "unknown error"
                )
            continue
        if ptype in {"warning", "context_compacted"}:
            text = str(
                payload.get("message")
                or payload.get("summary")
                or (
                    "Context was compacted"
                    if ptype == "context_compacted"
                    else "Warning"
                )
            )
            key = (
                current_turn.id if current_turn else None,
                "system",
                "commentary",
                text,
            )
            if key not in seen_messages:
                db.add(
                    CodexMessage(
                        id=_id(),
                        agent_session_id=session.id,
                        event_id=event.id,
                        turn_id=current_turn.id if current_turn else None,
                        role="system",
                        channel="commentary",
                        text=text,
                        markdown_text=text,
                        copy_text=text,
                        sequence=sequence,
                        timestamp=ts,
                        is_visible=True,
                        is_system_generated=True,
                    )
                )
                sequence += 1
                seen_messages.add(key)
            continue

        # --- token accounting ------------------------------------------------
        if top == "token_usage_record":
            turn_usage = _usage_snapshot(payload.get("turn_token_usage"))
            thread_usage = _usage_snapshot(payload.get("thread_token_usage"))
            response_usage = _usage_snapshot(payload.get("usage"))
            if thread_usage is not None:
                session_totals = thread_usage
            native_turn_id = str(payload.get("turn_id") or "")
            usage_turn = turns_by_native_id.get(native_turn_id, current_turn)
            response_id = str(payload.get("response_id") or "")
            response_key = (native_turn_id, response_id)
            duplicate_response = bool(response_id and response_key in seen_usage_responses)
            if response_id:
                seen_usage_responses.add(response_key)
            if usage_turn is not None:
                if turn_usage is not None:
                    # XDCode's turn snapshot is cumulative; assign instead of
                    # summing it with the same values from earlier records.
                    for key, value in turn_usage.items():
                        setattr(usage_turn, key, value)
                elif response_usage is not None and not duplicate_response:
                    for key, value in response_usage.items():
                        setattr(usage_turn, key, int(getattr(usage_turn, key) or 0) + value)
            continue
        if ptype == "token_count":
            if has_xdcode_usage_records:
                # The same file carries both ``token_count`` and XDCode's
                # authoritative cumulative ``token_usage_record`` stream.
                continue
            info = payload.get("info") if isinstance(payload.get("info"), dict) else payload
            total = info.get("total_token_usage") if isinstance(info, dict) else None
            last = info.get("last_token_usage") if isinstance(info, dict) else None
            if isinstance(total, dict):
                session_totals = total
            if current_turn is not None and isinstance(last, dict):
                current_turn.input_tokens = int(last.get("input_tokens") or 0)
                current_turn.cached_input_tokens = int(last.get("cached_input_tokens") or 0)
                current_turn.output_tokens = int(last.get("output_tokens") or 0)
                current_turn.reasoning_output_tokens = int(
                    last.get("reasoning_output_tokens") or 0
                )
                current_turn.total_tokens = int(last.get("total_tokens") or 0)
            continue

        # --- first-agent latency + exact item spans -------------------------
        item = payload.get("item") if isinstance(payload.get("item"), dict) else None
        item_type = str(item.get("type") or "").lower() if item is not None else ""
        agent_event = (
            ptype in {"agent_message", "agent_reasoning", "function_call", "custom_tool_call"}
            or (ptype == "message" and str(payload.get("role") or "") == "assistant")
            or item_type in _AGENT_ITEM_TYPES
        )
        event_started_at = _from_epoch_ms(payload.get("started_at_ms")) or ts
        if (
            agent_event
            and current_turn is not None
            and current_turn.first_agent_latency_ms is None
            and current_turn.started_at is not None
            and event_started_at is not None
        ):
            current_turn.first_agent_latency_ms = _elapsed_ms(
                current_turn.started_at, event_started_at
            )

        if ptype == "item_completed" and item is not None:
            started_at = _from_epoch_ms(payload.get("started_at_ms"))
            completed_at = _from_epoch_ms(payload.get("completed_at_ms")) or ts
            if started_at is not None:
                command = _item_command(item)
                span_type = _ITEM_SPAN_TYPES.get(item_type, "item")
                duration_ms = _elapsed_ms(started_at, completed_at)
                evidence_id = await add_rollout_evidence(
                    event,
                    category=f"activity_{span_type}",
                    summary=command or str(item.get("type") or "item"),
                    turn_id=current_turn.turn_id if current_turn else None,
                )
                source_record_id = f"event:{event.event_index}"
                db.add(
                    ActivitySpan(
                        id=_id(),
                        agent=session.agent,
                        source_kind="rollout",
                        source_record_id=source_record_id,
                        agent_session_id=session.id,
                        evidence_id=evidence_id,
                        thread_id=(
                            str(payload.get("thread_id"))
                            if payload.get("thread_id")
                            else session.thread_id
                        ),
                        turn_id=(
                            str(payload.get("turn_id"))
                            if payload.get("turn_id")
                            else current_turn.turn_id if current_turn else None
                        ),
                        span_type=span_type,
                        name=_bounded_label(command or str(item.get("type") or "item")),
                        status=_item_status(item),
                        started_at=started_at,
                        completed_at=completed_at,
                        duration_ms=duration_ms,
                        confidence="exact",
                        metadata_json={
                            "event_id": event.id,
                            "item_id": item.get("id"),
                            "item_type": item.get("type"),
                            "exit_code": item.get("exit_code"),
                        },
                    )
                )
                if (
                    current_turn is not None
                    and completed_at is not None
                    and span_type
                    in {
                        "tool",
                        "web_search",
                        "file_change",
                        "agent_tool",
                        "subagent",
                    }
                ):
                    tool_intervals.setdefault(current_turn.id, []).append(
                        (started_at, completed_at)
                    )
                if current_turn is not None and item_type == "commandexecution":
                    if command:
                        exact_tool_commands.add((current_turn.id, command.strip()))
                    if _is_validation_command(command):
                        validation_results.setdefault(current_turn.id, []).append(
                            item.get("exit_code") == 0
                        )
                    if _is_mutating("exec_command", command):
                        mutating_turns.add(current_turn.id)

        # --- messages --------------------------------------------------------
        projected = _message_projection(top, ptype, payload)
        if projected is not None:
            role, channel, text = projected
            stripped = text.strip()
            key = (
                current_turn.id if current_turn else None,
                role,
                channel,
                stripped,
            )
            if stripped and key not in seen_messages:
                if role == "user" and not session.display_title:
                    session.display_title = " ".join(stripped.split())[:255]
                db.add(
                    CodexMessage(
                        id=_id(),
                        agent_session_id=session.id,
                        event_id=event.id,
                        turn_id=current_turn.id if current_turn else None,
                        role=role,
                        channel=channel,
                        text=text,
                        markdown_text=_markdown_for(channel, text),
                        copy_text=text,
                        sequence=sequence,
                        timestamp=ts,
                        is_visible=True,
                    )
                )
                sequence += 1
                seen_messages.add(key)
            continue

        # --- tool calls ------------------------------------------------------
        if ptype in ("function_call", "custom_tool_call"):
            call_id = str(payload.get("call_id")) if payload.get("call_id") else f"idx-{event.event_index}"
            name = _bounded_label(payload.get("name") or "unknown")
            if ptype == "custom_tool_call":
                raw_input = payload.get("input")
                arguments: dict[str, Any] | None = raw_input if isinstance(raw_input, dict) else None
                command_text = _command_text(name, {"input": raw_input}) if isinstance(raw_input, str) else _command_text(name, arguments)
            else:
                raw_args = payload.get("arguments")
                parsed = _load_json(raw_args) if isinstance(raw_args, str) else raw_args
                arguments = parsed if isinstance(parsed, dict) else None
                command_text = _command_text(name, arguments)
            tool = ToolCall(
                id=_id(),
                agent=session.agent,
                agent_session_id=session.id,
                turn_id=current_turn.id if current_turn else None,
                event_id=event.id,
                call_id=call_id,
                tool_name=name,
                tool_kind=_tool_kind(name),
                arguments_json=arguments,
                command_text=redact_secrets(command_text),
                status="running",
                is_mutating=_is_mutating(name, command_text),
                started_at=ts,
            )
            db.add(tool)
            tools_by_call[call_id] = tool
            tool_evidence_ids[call_id] = await add_rollout_evidence(
                event,
                category=f"tool_{_tool_kind(name)}",
                summary=name,
                turn_id=current_turn.turn_id if current_turn else None,
            )
            if current_turn is not None and tool.is_mutating:
                mutating_turns.add(current_turn.id)
            if call_id in pending_outputs:
                output_text, code, completed_at = pending_outputs.pop(call_id)
                tool.output_text = redact_secrets(output_text)
                tool.exit_code = code
                tool.status = (
                    "failed"
                    if (code is not None and code != 0) or _looks_failed(output_text)
                    else "ok"
                )
                tool.completed_at = completed_at
            continue
        if ptype in ("function_call_output", "custom_tool_call_output"):
            call_id = str(payload.get("call_id") or "")
            tool = tools_by_call.get(call_id)
            out_text = _output_text(
                payload.get("output") or payload.get("aggregated_output")
            )
            code = _parse_exit_code(out_text)
            if tool is not None:
                tool.output_text = redact_secrets(out_text)
                tool.exit_code = code
                failed = (code is not None and code != 0) or _looks_failed(out_text)
                tool.status = "failed" if failed else "ok"
                tool.completed_at = ts or tool.completed_at
            else:
                pending_outputs[call_id] = (out_text, code, ts)
            continue
        if ptype in {
            "exec_command_end",
            "mcp_tool_call_end",
            "web_search_end",
        }:
            call_id = str(payload.get("call_id") or "")
            tool = tools_by_call.get(call_id)
            out_text = _output_text(
                payload.get("aggregated_output")
                or payload.get("output")
                or payload.get("result")
            )
            code_value = payload.get("exit_code")
            code = int(code_value) if isinstance(code_value, int) else _parse_exit_code(out_text)
            if tool is not None:
                tool.output_text = redact_secrets(out_text)
                tool.exit_code = code
                status_value = str(payload.get("status") or "")
                failed = (
                    status_value in {"failed", "error"}
                    or (code is not None and code != 0)
                    or _looks_failed(out_text)
                )
                tool.status = "failed" if failed else "ok"
                tool.completed_at = ts or tool.completed_at
            else:
                pending_outputs[call_id] = (out_text, code, ts)
            continue
        if ptype == "patch_apply_end":
            call_id = str(payload.get("call_id") or "")
            tool = tools_by_call.get(call_id)
            if tool is not None:
                success = bool(payload.get("success"))
                stdout = str(payload.get("stdout") or "")
                stderr = str(payload.get("stderr") or "")
                combined = "\n".join(part for part in (stdout, stderr) if part).strip()
                tool.output_text = combined or tool.output_text
                tool.exit_code = 0 if success else 1
                tool.status = "ok" if success else "failed"
                tool.completed_at = ts or tool.completed_at
                tool.is_mutating = True
            continue

    await flush_evidence()

    for tool in tools_by_call.values():
        if tool.status == "running":
            tool.status = "pending_or_missing_output"
        if tool.turn_id is not None and _is_validation_command(tool.command_text):
            validation_results.setdefault(tool.turn_id, []).append(
                tool.status == "ok" and (tool.exit_code in {None, 0})
            )
        turn_for_tool = next(
            (turn for turn in turns if turn.id == tool.turn_id),
            None,
        )
        exact_match = bool(
            turn_for_tool is not None
            and tool.command_text
            and (turn_for_tool.id, tool.command_text.strip()) in exact_tool_commands
        )
        if (
            not exact_match
            and tool.started_at is not None
            and tool.completed_at is not None
        ):
            if turn_for_tool is not None:
                tool_intervals.setdefault(turn_for_tool.id, []).append(
                    (tool.started_at, tool.completed_at)
                )
            db.add(
                ActivitySpan(
                    id=_id(),
                    agent=session.agent,
                    source_kind="rollout",
                    source_record_id=f"tool:{tool.call_id}",
                    agent_session_id=session.id,
                    evidence_id=tool_evidence_ids.get(tool.call_id),
                    thread_id=session.thread_id,
                    turn_id=turn_for_tool.turn_id if turn_for_tool else None,
                    span_type=tool.tool_kind,
                    name=tool.tool_name,
                    status=tool.status,
                    started_at=tool.started_at,
                    completed_at=tool.completed_at,
                    duration_ms=_elapsed_ms(tool.started_at, tool.completed_at),
                    confidence="derived",
                    metadata_json={
                        "tool_call_id": tool.call_id,
                        "is_mutating": tool.is_mutating,
                    },
                )
            )

    for turn in turns:
        if turn.duration_ms is None:
            turn.duration_ms = _elapsed_ms(turn.started_at, turn.completed_at)
        turn.active_tool_ms = _interval_union_ms(tool_intervals.get(turn.id, []))
        if turn.outcome == "unknown":
            turn.outcome = turn.status
        results = validation_results.get(turn.id, [])
        if results:
            turn.validation_status = "passed" if results[-1] else "failed"
        elif turn.id in mutating_turns:
            turn.validation_status = "unverified"
        else:
            turn.validation_status = "not_applicable"
        turn.metric_confidence = "derived"
        if turn.started_at is not None:
            db.add(
                ActivitySpan(
                    id=_id(),
                    agent=session.agent,
                    source_kind="rollout",
                    source_record_id=f"turn:{turn.turn_index}",
                    agent_session_id=session.id,
                    evidence_id=turn_evidence_ids.get(turn.id),
                    thread_id=session.thread_id,
                    turn_id=turn.turn_id,
                    span_type="turn",
                    name=f"Turn {turn.turn_index + 1}",
                    status=turn.status,
                    started_at=turn.started_at,
                    completed_at=turn.completed_at,
                    duration_ms=turn.duration_ms,
                    confidence=turn.metric_confidence,
                    metadata_json={
                        "turn_index": turn.turn_index,
                        "validation_status": turn.validation_status,
                    },
                )
            )

    await db.flush()

    # --- session-level aggregates -------------------------------------------
    session.turn_count = len(turns)
    if session_totals is not None:
        session.input_tokens = int(session_totals.get("input_tokens") or 0)
        session.cached_input_tokens = int(session_totals.get("cached_input_tokens") or 0)
        session.cache_creation_input_tokens = int(
            session_totals.get("cache_creation_input_tokens") or 0
        )
        session.output_tokens = int(session_totals.get("output_tokens") or 0)
        session.reasoning_output_tokens = int(session_totals.get("reasoning_output_tokens") or 0)
        session.total_tokens = int(session_totals.get("total_tokens") or 0)
    elif turns:
        session.input_tokens = sum(t.input_tokens for t in turns)
        session.cached_input_tokens = sum(t.cached_input_tokens for t in turns)
        session.cache_creation_input_tokens = sum(
            t.cache_creation_input_tokens for t in turns
        )
        session.output_tokens = sum(t.output_tokens for t in turns)
        session.reasoning_output_tokens = sum(t.reasoning_output_tokens for t in turns)
        session.total_tokens = sum(t.total_tokens for t in turns)
    _apply_session_status(session, turns, last_event_ts)
    session.projected_event_index = max_event_index
    await db.flush()
    return True


def serialize_event(event: AgentEvent) -> dict[str, Any]:
    """Serialize a raw event for the events API (matches the frontend contract)."""
    return {
        "event_index": event.event_index,
        "line_number": event.line_number,
        "byte_offset": event.byte_offset,
        "normalized_type": event.normalized_type,
        "top_level_type": event.top_level_type,
        "payload_type": event.payload_type,
        "is_known": event.is_known,
        "raw_json": redact_value(event.raw_json),
        "event_timestamp": event.event_timestamp.isoformat() if event.event_timestamp else None,
    }


def ordered_transcript_items(
    events: list[AgentEvent], messages: list[CodexMessage], tools: list[ToolCall]
) -> list[tuple[str, Any]]:
    """Interleave projected messages and tools back into chronological order.

    Both messages and tools carry the ``event_id`` they were projected from, so
    walking events by ``event_index`` yields a faithful transcript order.
    """
    msg_by_event = {m.event_id: m for m in messages if m.event_id is not None}
    tool_by_event = {t.event_id: t for t in tools if t.event_id is not None}
    items: list[tuple[str, Any]] = []
    for event in events:
        if event.id in msg_by_event:
            items.append(("message", msg_by_event[event.id]))
        elif event.id in tool_by_event:
            items.append(("tool", tool_by_event[event.id]))
    return items


_ROLE_LABEL = {"user": "User", "assistant": "Assistant", "system": "System", "developer": "Developer"}


def _message_label(message: CodexMessage) -> str:
    label = _ROLE_LABEL.get(message.role, message.role.title())
    if message.channel == "reasoning":
        return f"{label} (reasoning)"
    return label


def render_transcript_text(session: AgentSession, items: list[tuple[str, Any]]) -> str:
    """Plain-text transcript: messages + tool commands/outputs in order."""
    lines: list[str] = [f"{session.agent.title()} session {session.thread_id}"]
    if session.model:
        lines.append(f"Model: {session.model}")
    if session.cwd:
        lines.append(f"cwd: {session.cwd}")
    lines.append("")
    for kind, obj in items:
        if kind == "message":
            lines.append(f"{_message_label(obj).upper()}:")
            lines.append(obj.text)
        else:
            status = f" ({obj.status})" if obj.status else ""
            lines.append(f"TOOL {obj.tool_name}{status}:")
            if obj.command_text:
                lines.append(f"$ {obj.command_text}")
            if obj.output_text:
                lines.append(obj.output_text)
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def render_transcript_markdown(session: AgentSession, items: list[tuple[str, Any]]) -> str:
    """Structured Markdown transcript."""
    lines: list[str] = [f"# {session.agent.title()} session `{session.thread_id}`", ""]
    meta: list[str] = []
    if session.model:
        meta.append(f"- **Model:** {session.model}")
    if session.cwd:
        meta.append(f"- **cwd:** `{session.cwd}`")
    meta.append(f"- **Turns:** {session.turn_count}")
    meta.append(f"- **Total tokens:** {session.total_tokens}")
    meta.append(f"- **Ongoing:** {'yes' if session.is_ongoing else 'no'}")
    lines.extend(meta)
    lines.append("")
    for kind, obj in items:
        if kind == "message":
            if obj.channel == "reasoning":
                lines.append(f"### {_message_label(obj)}")
            else:
                lines.append(f"## {_message_label(obj)}")
            lines.append("")
            lines.append(obj.text)
            lines.append("")
        else:
            status = f" — {obj.status}" if obj.status else ""
            lines.append(f"#### Tool: `{obj.tool_name}`{status}")
            lines.append("")
            if obj.command_text:
                lines.append("```")
                lines.append(obj.command_text)
                lines.append("```")
            if obj.output_text:
                lines.append("```")
                lines.append(obj.output_text)
                lines.append("```")
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def render_transcript_json(events: list[AgentEvent]) -> str:
    """The full ordered raw event list, serialized as pretty JSON text."""
    return json.dumps(
        [redact_value(event.raw_json) for event in events],
        ensure_ascii=False,
        indent=2,
    )


def escape_like(term: str) -> str:
    """Escape LIKE/ILIKE wildcards so a query is matched literally."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def make_snippet(text: str | None, query: str, radius: int = 80) -> str:
    """A one-line context window around the first case-insensitive match."""
    if not text:
        return ""
    low = text.lower()
    idx = low.find(query.lower())
    if idx < 0:
        window = text[: radius * 2]
        return " ".join(window.split())
    start = max(0, idx - radius)
    end = min(len(text), idx + len(query) + radius)
    snippet = " ".join(text[start:end].split())
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return f"{prefix}{snippet}{suffix}"


async def search_session(
    db: AsyncSession, session_id: str, query: str, limit: int
) -> list[dict[str, Any]]:
    """ILIKE search over message text and tool command/output; messages rank first."""
    pattern = f"%{escape_like(query)}%"
    matches: list[dict[str, Any]] = []

    message_rows = list(
        (
            await db.scalars(
                select(CodexMessage)
                .where(
                    CodexMessage.agent_session_id == session_id,
                    CodexMessage.text.ilike(pattern, escape="\\"),
                )
                .order_by(CodexMessage.sequence)
                .limit(limit)
            )
        ).all()
    )
    for message in message_rows:
        matches.append(
            {
                "kind": "message",
                "id": message.id,
                "ref": str(message.sequence),
                "snippet": make_snippet(message.text, query),
            }
        )

    if len(matches) < limit:
        tool_rows = list(
            (
                await db.scalars(
                    select(ToolCall)
                    .where(
                        ToolCall.agent_session_id == session_id,
                        or_(
                            ToolCall.command_text.ilike(pattern, escape="\\"),
                            ToolCall.output_text.ilike(pattern, escape="\\"),
                        ),
                    )
                    .order_by(ToolCall.started_at, ToolCall.created_at)
                    .limit(limit - len(matches))
                )
            ).all()
        )
        for tool in tool_rows:
            command = tool.command_text or ""
            source = command if query.lower() in command.lower() else (tool.output_text or "")
            matches.append(
                {
                    "kind": "tool",
                    "id": tool.id,
                    "ref": tool.call_id,
                    "snippet": make_snippet(source, query),
                }
            )

    return matches[:limit]
