"""Pi native session adapter with branch-aware projection."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_observatory.db.models import (
    ActivitySpan,
    AgentEvent,
    AgentSession,
    AgentTurn,
    CodexMessage,
    ToolCall,
)

from .adapters import PlanAction, SourceDescriptor, SourcePlan
from .fingerprints import file_fingerprint, indexed_content_matches
from .pi_parser import PiRecord, content_blocks, iter_records

_ONGOING_MTIME_SECONDS = 60
_TOOL_KINDS = {
    "bash": "exec",
    "shell": "exec",
    "exec": "exec",
    "exec_command": "exec",
    "read": "read",
    "write": "write",
    "edit": "patch",
    "apply_patch": "patch",
    "web_search": "web_search",
    "fetch": "fetch",
    "browser": "browser",
}
_MUTATING_TOOLS = {"write", "edit", "apply_patch"}


def _id(prefix: str, value: str) -> str:
    return hashlib.sha256(f"{prefix}:{value}".encode()).hexdigest()[:48]


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.replace("\x00", "\\u0000")
    if isinstance(value, list):
        return "\n".join(_text(item) for item in value)
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return _text(value["text"])
        if isinstance(value.get("content"), (str, list)):
            return _text(value["content"])
        if isinstance(value.get("thinking"), str):
            return _text(value["thinking"])
    return str(value).replace("\x00", "\\u0000") if value is not None else ""


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _usage(message: dict[str, Any]) -> dict[str, int]:
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return {}
    result = {
        "input_tokens": _as_int(usage.get("input")),
        "cached_input_tokens": _as_int(usage.get("cacheRead")),
        "cache_creation_input_tokens": _as_int(usage.get("cacheWrite")),
        "output_tokens": _as_int(usage.get("output")),
        # Pi includes reasoning output in ``output``; keep it visible but do
        # not add it to the total a second time.
        "reasoning_output_tokens": _as_int(usage.get("reasoning")),
    }
    result["total_tokens"] = _as_int(usage.get("totalTokens")) or sum(
        result[key]
        for key in (
            "input_tokens",
            "cached_input_tokens",
            "cache_creation_input_tokens",
            "output_tokens",
        )
    )
    return result


def _exit_code(details: Any) -> int | None:
    if not isinstance(details, dict):
        return None
    for key in ("exitCode", "exit_code", "code"):
        value = details.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


def _tool_kind(name: str) -> str:
    normalized = name.strip().lower()
    if normalized in _TOOL_KINDS:
        return _TOOL_KINDS[normalized]
    if normalized.startswith("shell") or normalized.startswith("exec"):
        return "exec"
    return normalized[:64] or "tool"


def _session_name(raw: dict[str, Any]) -> str | None:
    record_type = str(raw.get("type") or "")
    if record_type not in {"session", "session_name", "session_info", "custom"}:
        return None
    data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    for key in ("name", "title", "sessionName", "session_name"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:255]
    return None


def _summary_text(raw: dict[str, Any]) -> str | None:
    data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    for key in ("summary", "text", "message", "content"):
        value = data.get(key)
        if isinstance(value, (str, list, dict)):
            text = _text(value).strip()
            if text:
                return text
    return None


def _assistant_record(raw: dict[str, Any]) -> dict[str, Any] | None:
    message = raw.get("message")
    if isinstance(message, dict) and message.get("role") == "assistant":
        return message
    return None


class PiJsonlSource:
    kind = "pi_jsonl"
    agent = "pi"

    def __init__(
        self, root: str | Path, *, parser_version: str = "2", agent: str = "pi"
    ) -> None:
        self.root = Path(root).expanduser()
        self.parser_version = parser_version
        self.agent = agent

    @staticmethod
    def identity(path: Path) -> str:
        digest = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
        return f"pi:{digest}"

    def discover(self) -> list[SourceDescriptor]:
        if not self.root.exists():
            return []
        paths = sorted(
            (path for path in self.root.rglob("*.jsonl") if path.is_file()),
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
        stat = source.path.stat()
        fingerprint = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "path": str(source.path.resolve()),
        }
        if checkpoint is None or checkpoint.parser_version != self.parser_version:
            return SourcePlan(source, PlanAction.REBUILD, file_fingerprint(source.path))
        previous = checkpoint.fingerprint_json or {}
        cursor = checkpoint.cursor_json or {}
        if stat.st_size < _as_int(cursor.get("byte_offset")):
            return SourcePlan(source, PlanAction.REBUILD, fingerprint)
        if (
            checkpoint.status == "indexed"
            and stat.st_size == previous.get("size")
            and stat.st_mtime_ns == previous.get("mtime_ns")
        ):
            return SourcePlan(source, PlanAction.UNCHANGED, fingerprint)
        fingerprint = file_fingerprint(source.path)
        action = (
            PlanAction.REBUILD
            if stat.st_size == previous.get("size")
            or not indexed_content_matches(source.path, previous)
            else PlanAction.APPEND
        )
        return SourcePlan(source, action, fingerprint)

    async def ingest(self, db: AsyncSession, plan: SourcePlan) -> AgentSession:
        source = plan.source
        real_path = str(source.path.resolve())
        session = await db.scalar(
            select(AgentSession).where(
                AgentSession.source_identity == source.identity
            )
        )
        if session is None:
            session = AgentSession(
                id=_id("session", source.identity),
                thread_id=source.path.stem,
                rollout_path=str(source.path),
                rollout_real_path=real_path,
                rollout_format="jsonl",
                agent=self.agent,
                source_kind=self.kind,
                source_identity=source.identity,
                source_path=real_path,
                source_format="jsonl",
                parser_version=self.parser_version,
                index_status="discovered",
            )
            db.add(session)
            await db.flush()
        else:
            session.agent = self.agent
            session.source_kind = self.kind
            session.source_path = real_path
            session.rollout_path = str(source.path)
            session.rollout_real_path = real_path

        if plan.action == PlanAction.UNCHANGED:
            return session
        plan.fingerprint.update(file_fingerprint(source.path))
        if plan.action == PlanAction.REBUILD or session.parser_version != self.parser_version:
            await self._clear(db, session.id)
            session.last_indexed_offset = 0
            session.last_indexed_line = 0
            session.parser_version = self.parser_version
            session.turn_count = 0
            session.input_tokens = 0
            session.cached_input_tokens = 0
            session.cache_creation_input_tokens = 0
            session.output_tokens = 0
            session.reasoning_output_tokens = 0
            session.total_tokens = 0
            session.projected_event_index = -1
            session.model = None
            session.cwd = None
            session.cli_version = None
            session.thread_id = source.path.stem
            session.display_title = None
            session.thread_name = None
            session.start_time = None
            session.end_time = None
            session.last_activity_at = None

        last_event_index = await db.scalar(
            select(func.max(AgentEvent.event_index)).where(
                AgentEvent.agent_session_id == session.id
            )
        )
        event_index = (last_event_index if last_event_index is not None else -1) + 1
        session.index_status = "indexing"
        session.last_error = None
        pending_bytes = 0
        for record in iter_records(
            source.path,
            offset=_as_int(session.last_indexed_offset),
            line_offset=_as_int(session.last_indexed_line),
        ):
            raw = record.raw_json
            event = AgentEvent(
                agent_session_id=session.id,
                event_index=event_index,
                line_number=record.line_number,
                byte_offset=record.byte_offset,
                event_timestamp=record.event_timestamp,
                top_level_type=record.record_type,
                payload_type=record.payload_type,
                normalized_type=record.record_type if record.known else "unknown",
                raw_json=raw,
                raw_hash=record.raw_hash,
                is_known=record.known,
                parser_version=self.parser_version,
            )
            db.add(event)
            if record.record_type == "session":
                self._apply_metadata(session, record)
            pending_bytes += record.byte_length
            event_index += 1
            session.last_indexed_line = record.line_number
            session.last_indexed_offset = record.byte_offset + record.byte_length
            if record.record_type == "parse_error":
                session.index_status = "parse_error"
                session.last_error = str(raw.get("error") or "parse_error")[:500]
            if event_index % 250 == 0 or pending_bytes >= 4 * 1024 * 1024:
                await db.flush()
                pending_bytes = 0

        session.file_size = plan.fingerprint["size"]
        session.file_mtime_ns = plan.fingerprint["mtime_ns"]
        if session.index_status != "parse_error":
            session.index_status = "indexed"
        await db.flush()
        await self._project(db, session)
        return session

    @staticmethod
    def _apply_metadata(session: AgentSession, record: PiRecord) -> None:
        raw = record.raw_json
        record_type = record.record_type
        if record_type == "session":
            session.thread_id = str(raw.get("id") or session.thread_id)
            if isinstance(raw.get("cwd"), str):
                session.cwd = raw["cwd"]
            if raw.get("version") is not None:
                session.cli_version = str(raw["version"])[:64]
            session.start_time = record.event_timestamp or session.start_time
            name = _session_name(raw)
            if name:
                session.thread_name = name
        elif record_type == "model_change":
            for key in ("modelId", "model"):
                if isinstance(raw.get(key), str):
                    session.model = raw[key][:255]
                    break
        elif record_type == "message":
            message = raw.get("message")
            if isinstance(message, dict):
                if isinstance(message.get("model"), str):
                    session.model = message["model"][:255]
                if session.start_time is None:
                    session.start_time = record.event_timestamp
                if (
                    session.display_title is None
                    and message.get("role") == "user"
                ):
                    for block in content_blocks(record):
                        if block.get("type") == "text":
                            text = _text(block.get("text")).strip()
                            if text:
                                session.display_title = text[:255]
                                break
        name = _session_name(raw)
        if name:
            session.thread_name = name
        if record_type in {"session_end", "session_shutdown"}:
            session.end_time = record.event_timestamp or session.end_time

    async def _clear(self, db: AsyncSession, session_id: str) -> None:
        for model in (ActivitySpan, CodexMessage, ToolCall, AgentTurn, AgentEvent):
            await db.execute(
                delete(model)
                .where(model.agent_session_id == session_id)
                .execution_options(synchronize_session=False)
            )

    async def _project(self, db: AsyncSession, session: AgentSession) -> None:
        maximum = await db.scalar(
            select(func.max(AgentEvent.event_index)).where(
                AgentEvent.agent_session_id == session.id
            )
        )
        if maximum is None:
            return
        if maximum == session.projected_event_index:
            return
        for model in (ActivitySpan, CodexMessage, ToolCall, AgentTurn):
            await db.execute(
                delete(model)
                .where(model.agent_session_id == session.id)
                .execution_options(synchronize_session="evaluate")
            )
        nodes = await db.stream(
            select(
                AgentEvent.line_number,
                AgentEvent.event_index,
                AgentEvent.raw_json["id"].astext.label("record_id"),
                AgentEvent.raw_json["parentId"].astext.label("parent_id"),
                AgentEvent.raw_json["type"].astext.label("record_type"),
            )
            .where(AgentEvent.agent_session_id == session.id)
            .order_by(AgentEvent.event_index)
            .execution_options(yield_per=500)
        )
        by_id: dict[str, tuple[int, str | None, str | None]] = {}
        all_lines: set[int] = set()
        async for node in nodes:
            all_lines.add(node.line_number)
            if node.record_id is not None:
                by_id[node.record_id] = (node.line_number, node.parent_id, node.record_type)
        active_lines = self._active_branch_lines(by_id, all_lines)
        session.model = None
        session.thread_name = None
        session.display_title = None

        turns: list[AgentTurn] = []
        current_turn: AgentTurn | None = None
        tools: dict[str, ToolCall] = {}
        sequence = 0
        last_timestamp: datetime | None = session.start_time
        last_assistant_stop: str | None = None
        last_record_was_tool_result = False
        explicit_end = False

        def add_message(
            *,
            event: AgentEvent,
            role: str,
            channel: str,
            text: str,
            timestamp: datetime | None,
            system: bool = False,
        ) -> None:
            nonlocal sequence
            if not text:
                return
            db.add(
                CodexMessage(
                    id=_id("msg", f"{session.id}:{event.event_index}:{sequence}"),
                    agent_session_id=session.id,
                    event_id=event.id,
                    turn_id=current_turn.id if current_turn else None,
                    role=role,
                    channel=channel,
                    text=text,
                    markdown_text=text,
                    copy_text=text,
                    sequence=sequence,
                    timestamp=timestamp,
                    is_visible=channel != "reasoning",
                    is_system_generated=system,
                )
            )
            sequence += 1

        def finish_turn(timestamp: datetime | None, *, status: str = "completed") -> None:
            nonlocal current_turn
            if current_turn is None:
                return
            if current_turn.completed_at is None:
                current_turn.completed_at = timestamp
            current_turn.status = status
            current_turn.outcome = status
            if current_turn.started_at and current_turn.completed_at:
                current_turn.duration_ms = int(
                    (current_turn.completed_at - current_turn.started_at).total_seconds()
                    * 1000
                )
            current_turn = None

        events = await db.stream_scalars(
            select(AgentEvent)
            .where(AgentEvent.agent_session_id == session.id)
            .order_by(AgentEvent.event_index)
            .execution_options(yield_per=10)
        )
        projected_count = 0
        async for event in events:
            if event.line_number not in active_lines:
                continue
            projected_count += 1
            if projected_count % 25 == 0:
                await db.flush()
            self._apply_metadata(session, _record_from_event(event))
            raw = event.raw_json
            record_type = str(raw.get("type") or "")
            ts = event.event_timestamp or last_timestamp
            if ts is not None:
                last_timestamp = ts
            if record_type in {"session_end", "session_shutdown"}:
                explicit_end = True
                continue
            if record_type in {"compaction", "compaction_summary", "branch_summary"}:
                text = _summary_text(raw)
                if text:
                    add_message(
                        event=event,
                        role="system",
                        channel="commentary",
                        text=text,
                        timestamp=ts,
                        system=True,
                    )
                continue
            if record_type in {"model_change", "thinking_level_change", "session_name", "session_info"}:
                continue
            if record_type != "message":
                continue
            message = raw.get("message")
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "")
            if role == "user":
                if current_turn is not None:
                    finish_turn(ts)
                current_turn = AgentTurn(
                    id=_id("turn", f"{session.id}:{len(turns)}"),
                    agent=self.agent,
                    agent_session_id=session.id,
                    turn_index=len(turns),
                    turn_id=str(raw.get("id") or "") or None,
                    status="running",
                    started_at=ts,
                    input_tokens=0,
                    cached_input_tokens=0,
                    cache_creation_input_tokens=0,
                    output_tokens=0,
                    reasoning_output_tokens=0,
                    total_tokens=0,
                    outcome="running",
                )
                db.add(current_turn)
                turns.append(current_turn)
                for block in content_blocks(_record_from_event(event)):
                    if block.get("type") == "text" and _text(block.get("text")).strip():
                        add_message(
                            event=event,
                            role="user",
                            channel="final",
                            text=_text(block.get("text")),
                            timestamp=ts,
                        )
                last_record_was_tool_result = False
                last_assistant_stop = None
                continue
            if role == "system":
                for block in content_blocks(_record_from_event(event)):
                    if block.get("type") == "text":
                        add_message(
                            event=event,
                            role="system",
                            channel="commentary",
                            text=_text(block.get("text")),
                            timestamp=ts,
                            system=True,
                        )
                continue
            if role == "assistant":
                if current_turn is None:
                    current_turn = AgentTurn(
                        id=_id("turn", f"{session.id}:{len(turns)}"),
                        agent=self.agent,
                        agent_session_id=session.id,
                        turn_index=len(turns),
                        turn_id=str(raw.get("id") or "") or None,
                        status="running",
                        started_at=ts,
                        input_tokens=0,
                        cached_input_tokens=0,
                        cache_creation_input_tokens=0,
                        output_tokens=0,
                        reasoning_output_tokens=0,
                        total_tokens=0,
                        outcome="running",
                    )
                    db.add(current_turn)
                    turns.append(current_turn)
                usage = _usage(message)
                for key in (
                    "input_tokens",
                    "cached_input_tokens",
                    "cache_creation_input_tokens",
                    "output_tokens",
                    "reasoning_output_tokens",
                    "total_tokens",
                ):
                    setattr(current_turn, key, getattr(current_turn, key) + usage.get(key, 0))
                for block in content_blocks(_record_from_event(event)):
                    block_type = str(block.get("type") or "")
                    if block_type == "thinking":
                        text = _text(block.get("thinking") or block.get("text"))
                        add_message(
                            event=event,
                            role="assistant",
                            channel="reasoning",
                            text=text,
                            timestamp=ts,
                        )
                    elif block_type == "text":
                        text = _text(block.get("text"))
                        if text.strip():
                            add_message(
                                event=event,
                                role="assistant",
                                channel="final",
                                text=text,
                                timestamp=ts,
                            )
                    elif block_type == "toolCall":
                        call_id = str(block.get("id") or f"{event.event_index}:{sequence}")
                        name = str(block.get("name") or "unknown")[:255]
                        arguments = (
                            block.get("arguments")
                            if isinstance(block.get("arguments"), dict)
                            else None
                        )
                        command = None
                        if arguments:
                            for key in ("command", "cmd", "path", "file_path"):
                                if isinstance(arguments.get(key), str):
                                    command = arguments[key]
                                    break
                            command = command or json.dumps(
                                arguments, ensure_ascii=False, default=str
                            )
                        tool = ToolCall(
                            id=_id("tool", f"{session.id}:{call_id}"),
                            agent=self.agent,
                            agent_session_id=session.id,
                            turn_id=current_turn.id if current_turn else None,
                            event_id=event.id,
                            call_id=call_id,
                            tool_name=name,
                            tool_kind=_tool_kind(name),
                            arguments_json=arguments,
                            command_text=command,
                            started_at=ts,
                            status="pending",
                            is_mutating=(
                                name.lower() in _MUTATING_TOOLS
                                or bool(
                                    command
                                    and any(
                                        marker in command
                                        for marker in (" > ", " >> ", "rm ", "mv ", "git apply")
                                    )
                                )
                            ),
                        )
                        db.add(tool)
                        tools[call_id] = tool
                stop = message.get("stopReason")
                if stop is not None:
                    last_assistant_stop = str(stop)
                last_record_was_tool_result = False
                continue
            if role == "toolResult":
                call_id = str(message.get("toolCallId") or "")
                tool = tools.get(call_id)
                if tool is None:
                    continue
                output = _text(message.get("content") or message.get("details"))
                tool.output_text = output
                tool.completed_at = ts
                tool.exit_code = _exit_code(message.get("details"))
                tool.status = "failed" if message.get("isError") or tool.exit_code not in (None, 0) else "ok"
                if tool.status == "failed" and tool.exit_code is None:
                    tool.exit_code = 1
                last_record_was_tool_result = True
                tools.pop(call_id, None)

        recent = (
            session.file_mtime_ns > 0
            and (datetime.now(UTC) - datetime.fromtimestamp(
                session.file_mtime_ns / 1_000_000_000, tz=UTC
            )).total_seconds()
            <= _ONGOING_MTIME_SECONDS
        )
        ongoing = bool(
            not explicit_end
            and recent
            and (
                last_record_was_tool_result
                or last_assistant_stop in {"toolUse", "tool_use", "error", None}
            )
        )
        failed = last_assistant_stop in {"error", "aborted", "length"}
        if current_turn is not None and not ongoing:
            finish_turn(last_timestamp, status="failed" if failed else "completed")
        elif current_turn is not None and failed:
            current_turn.status = "failed"
            current_turn.outcome = "failed"

        for turn in turns:
            if turn.status == "unknown":
                turn.outcome = "unknown"
            if turn.started_at is not None:
                db.add(
                    ActivitySpan(
                        id=_id("span", f"turn:{turn.id}"),
                        agent=self.agent,
                        source_kind=self.kind,
                        source_record_id=f"turn:{turn.turn_index}",
                        agent_session_id=session.id,
                        thread_id=session.thread_id,
                        turn_id=turn.turn_id,
                        span_type="turn",
                        name=f"Turn {turn.turn_index + 1}",
                        status=turn.status,
                        started_at=turn.started_at,
                        completed_at=turn.completed_at,
                        duration_ms=turn.duration_ms,
                        confidence="derived",
                        metadata_json={"agent": self.agent},
                    )
                )
        await db.flush()
        projected_tools = await db.stream(
            select(
                ToolCall.id, ToolCall.call_id, ToolCall.turn_id,
                ToolCall.started_at, ToolCall.completed_at, ToolCall.tool_kind,
                ToolCall.tool_name, ToolCall.status, ToolCall.is_mutating,
            )
            .where(ToolCall.agent_session_id == session.id)
            .execution_options(yield_per=100)
        )
        span_count = 0
        async for tool in projected_tools:
            if tool.started_at is None:
                continue
            db.add(
                ActivitySpan(
                    id=_id("span", f"tool:{tool.id}"),
                    agent=self.agent,
                    source_kind=self.kind,
                    source_record_id=f"tool:{tool.call_id}",
                    agent_session_id=session.id,
                    thread_id=session.thread_id,
                    turn_id=tool.turn_id,
                    span_type=tool.tool_kind,
                    name=tool.tool_name,
                    status=tool.status,
                    started_at=tool.started_at,
                    completed_at=tool.completed_at,
                    duration_ms=(
                        int((tool.completed_at - tool.started_at).total_seconds() * 1000)
                        if tool.completed_at
                        else None
                    ),
                    confidence="derived",
                    metadata_json={"agent": self.agent, "is_mutating": tool.is_mutating},
                )
            )
            span_count += 1
            if span_count % 250 == 0:
                await db.flush()

        session.turn_count = len(turns)
        session.input_tokens = sum(turn.input_tokens for turn in turns)
        session.cached_input_tokens = sum(turn.cached_input_tokens for turn in turns)
        session.cache_creation_input_tokens = sum(
            turn.cache_creation_input_tokens for turn in turns
        )
        session.output_tokens = sum(turn.output_tokens for turn in turns)
        session.reasoning_output_tokens = sum(
            turn.reasoning_output_tokens for turn in turns
        )
        session.total_tokens = sum(turn.total_tokens for turn in turns)
        session.is_ongoing = ongoing
        session.end_time = None if ongoing else (last_timestamp or session.end_time)
        session.last_activity_at = last_timestamp or session.start_time
        session.projected_event_index = maximum
        await db.flush()

    @staticmethod
    def _active_branch_lines(
        by_id: dict[str, tuple[int, str | None, str | None]], all_lines: set[int]
    ) -> set[int]:
        """Return line numbers on the current (last-leaf) Pi branch.

        The raw event trail remains complete in PostgreSQL.  Projection follows
        only the branch ending at the last stored entry, preventing abandoned
        branches from being concatenated into one conversation.
        """
        parents = {node[1] for node in by_id.values() if node[1] is not None}
        leaves = [
            (key, node)
            for key, node in by_id.items()
            if key not in parents and node[2] != "session"
        ]
        if not leaves:
            return all_lines
        key, current = max(leaves, key=lambda item: item[1][0])
        selected: set[int] = set()
        seen: set[str] = set()
        while current is not None:
            if key in seen:
                break
            seen.add(key)
            selected.add(current[0])
            key = current[1]
            current = by_id.get(key) if key is not None else None
        return selected or all_lines


def _record_from_event(event: AgentEvent) -> PiRecord:
    raw = event.raw_json
    return PiRecord(
        line_number=event.line_number,
        byte_offset=event.byte_offset,
        byte_length=0,
        raw_json=raw,
        raw_hash=event.raw_hash,
        event_timestamp=event.event_timestamp,
    )


__all__ = ["PiJsonlSource"]
