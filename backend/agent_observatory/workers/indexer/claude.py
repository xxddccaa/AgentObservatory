"""Claude Code JSONL source adapter and projection."""
from __future__ import annotations

import hashlib
from contextlib import suppress
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
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
from .claude_parser import ClaudeRecord, content_blocks, iter_records


def _id(prefix: str, value: str) -> str:
    return hashlib.sha256(f"{prefix}:{value}".encode()).hexdigest()[:48]


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_text(v) for v in value)
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        if isinstance(value.get("content"), (str, list)):
            return _text(value["content"])
    return str(value) if value is not None else ""


def _usage(message: dict[str, Any]) -> dict[str, int]:
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return {}
    aliases = {
        "input_tokens": ("input_tokens",),
        "cached_input_tokens": ("cache_read_input_tokens", "cached_input_tokens"),
        "cache_creation_input_tokens": ("cache_creation_input_tokens",),
        "output_tokens": ("output_tokens",),
        "reasoning_output_tokens": ("reasoning_output_tokens",),
    }
    result: dict[str, int] = {}
    for target, keys in aliases.items():
        for key in keys:
            if usage.get(key) is not None:
                with suppress(TypeError, ValueError):
                    result[target] = int(usage[key])
                break
    result["total_tokens"] = sum(
        result.get(k, 0)
        for k in (
            "input_tokens",
            "cached_input_tokens",
            "cache_creation_input_tokens",
            "output_tokens",
        )
    )
    details = usage.get("output_tokens_details")
    if isinstance(details, dict) and details.get("thinking_tokens") is not None:
        with suppress(TypeError, ValueError):
            result["reasoning_output_tokens"] = int(details["thinking_tokens"])
    return result


class ClaudeJsonlSource:
    kind = "claude_jsonl"

    def __init__(self, root: str | Path, *, agent: str, parser_version: str = "1") -> None:
        if agent not in {"claude", "stepcode"}:
            raise ValueError("ClaudeJsonlSource agent must be 'claude' or 'stepcode'")
        self.root = Path(root).expanduser()
        self.agent = agent
        self.parser_version = parser_version

    @staticmethod
    def identity_for(path: Path, agent: str) -> str:
        canonical = str(path.resolve())
        digest = hashlib.sha256(f"{agent}\0{canonical}".encode()).hexdigest()
        return f"claude:{agent}:{digest}"

    @staticmethod
    def identity(path: Path, agent: str = "claude") -> str:
        return ClaudeJsonlSource.identity_for(path, agent)

    @staticmethod
    def parent_session_from_path(path: Path) -> str | None:
        """Extract parent thread id from ``.../<thread>/subagents/...``."""
        parts = path.resolve().parts
        try:
            idx = parts.index("subagents")
        except ValueError:
            return None
        return parts[idx - 1] if idx > 0 else None

    def discover(self) -> list[SourceDescriptor]:
        if not self.root.exists():
            return []
        paths = sorted((p for p in self.root.rglob("*.jsonl") if p.is_file()), key=lambda p: str(p))
        return [
            SourceDescriptor(
                kind=self.kind,
                identity=self.identity_for(path, self.agent),
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
            "agent": self.agent,
        }
        if checkpoint is None or checkpoint.parser_version != self.parser_version:
            return SourcePlan(source, PlanAction.REBUILD, fingerprint)
        previous = checkpoint.fingerprint_json or {}
        cursor = checkpoint.cursor_json or {}
        if previous.get("agent") != self.agent or previous.get("path") != fingerprint["path"]:
            return SourcePlan(source, PlanAction.REBUILD, fingerprint)
        if stat.st_size < int(cursor.get("byte_offset", 0)):
            return SourcePlan(source, PlanAction.REBUILD, fingerprint)
        if (
            stat.st_size == previous.get("size")
            and stat.st_mtime_ns == previous.get("mtime_ns")
            and checkpoint.status == "indexed"
        ):
            return SourcePlan(source, PlanAction.UNCHANGED, fingerprint)
        # Same-size edits are conservatively rebuilt; append is only selected
        # when the file grew beyond the last indexed byte.
        action = PlanAction.APPEND if stat.st_size > int(previous.get("size", 0)) else PlanAction.REBUILD
        return SourcePlan(source, action, fingerprint)

    async def ingest(self, db: AsyncSession, plan: SourcePlan) -> AgentSession:
        source = plan.source
        real_path = str(source.path.resolve())
        session = await db.scalar(select(AgentSession).where(AgentSession.source_identity == source.identity))
        if session is None:
            session = AgentSession(
                id=_id("session", source.identity),
                thread_id=source.path.stem,
                rollout_path=str(source.path),
                rollout_real_path=f"{self.agent}:{real_path}",
                rollout_format="jsonl",
                source_kind=self.kind,
                source_identity=source.identity,
                source_path=real_path,
                source_format="jsonl",
                agent=self.agent,
                parser_version=self.parser_version,
            )
            db.add(session)
            await db.flush()
        else:
            session.agent = self.agent
            session.source_path = real_path
            session.rollout_path = str(source.path)
            session.rollout_real_path = f"{self.agent}:{real_path}"

        # Claude/StepCode delegated runs are stored below
        # ``<parent-thread>/subagents/agent-*.jsonl``.  Capture this
        # source-level relationship so History can hide children by default.
        parent_id = self.parent_session_from_path(source.path)
        if parent_id and parent_id != session.thread_id:
            session.parent_session_id = parent_id
            session.is_subagent = True

        if plan.action == PlanAction.UNCHANGED:
            return session
        if plan.action == PlanAction.REBUILD:
            await self._clear(db, session.id)
            session.last_indexed_offset = 0
            session.last_indexed_line = 0

        offset = int(session.last_indexed_offset or 0)
        line_offset = int(session.last_indexed_line or 0)
        event_index = int(
            await db.scalar(
                select(AgentEvent.event_index)
                .where(AgentEvent.agent_session_id == session.id)
                .order_by(AgentEvent.event_index.desc())
                .limit(1)
            )
            or -1
        ) + 1
        for record in iter_records(source.path, offset=offset, line_offset=line_offset):
            raw = record.raw_json
            payload = raw.get("message") if isinstance(raw.get("message"), dict) else raw
            event = AgentEvent(
                agent_session_id=session.id,
                event_index=event_index,
                line_number=record.line_number,
                byte_offset=record.byte_offset,
                event_timestamp=record.timestamp,
                top_level_type=record.record_type,
                payload_type=str(payload.get("role") or record.record_type) if isinstance(payload, dict) else record.record_type,
                normalized_type=record.record_type if record.record_type in {"user", "assistant", "summary", "attachment"} else "unknown",
                raw_json=raw,
                raw_hash=record.raw_hash,
                is_known=record.record_type in {"user", "assistant", "summary", "attachment"},
                parser_version=self.parser_version,
            )
            db.add(event)
            event_index += 1
            session.last_indexed_line = record.line_number
            session.last_indexed_offset = record.byte_offset + record.byte_length
            if session.start_time is None and record.timestamp is not None:
                session.start_time = record.timestamp
            for key, attr in (
                ("cwd", "cwd"),
                ("model", "model"),
                ("version", "cli_version"),
                ("gitBranch", "git_branch"),
                ("git_branch", "git_branch"),
            ):
                if getattr(session, attr) is None and isinstance(raw.get(key), str):
                    setattr(session, attr, raw[key])
            message_meta = raw.get("message")
            if (
                session.model is None
                and isinstance(message_meta, dict)
                and isinstance(message_meta.get("model"), str)
            ):
                session.model = message_meta["model"]
            if isinstance(raw.get("slug"), str) and not session.thread_name:
                session.thread_name = raw["slug"]
            if record.record_type == "user" and not session.display_title:
                for block in content_blocks(record):
                    if block.get("type") == "text" and _text(block.get("text")).strip():
                        session.display_title = _text(block.get("text")).strip()[:255]
                        break
            if isinstance(raw.get("sessionId"), str):
                session.thread_id = raw["sessionId"]
        await db.flush()
        stat = source.path.stat()
        session.file_size = stat.st_size
        session.file_mtime_ns = stat.st_mtime_ns
        session.index_status = "indexed"
        await self._project(db, session)
        return session

    async def _clear(self, db: AsyncSession, session_id: str) -> None:
        for model in (ActivitySpan, CodexMessage, ToolCall, AgentTurn, AgentEvent):
            await db.execute(delete(model).where(model.agent_session_id == session_id))

    async def _project(self, db: AsyncSession, session: AgentSession) -> None:
        await db.execute(delete(ActivitySpan).where(ActivitySpan.agent_session_id == session.id))
        await db.execute(delete(CodexMessage).where(CodexMessage.agent_session_id == session.id))
        await db.execute(delete(ToolCall).where(ToolCall.agent_session_id == session.id))
        await db.execute(delete(AgentTurn).where(AgentTurn.agent_session_id == session.id))
        events = (
            await db.scalars(
                select(AgentEvent)
                .where(AgentEvent.agent_session_id == session.id)
                .order_by(AgentEvent.event_index)
            )
        ).all()
        current: AgentTurn | None = None
        turns: list[AgentTurn] = []
        tools: dict[str, ToolCall] = {}
        sequence = 0
        for event in events:
            raw = event.raw_json
            typ = event.top_level_type
            blocks = content_blocks(ClaudeRecord(event.line_number, event.byte_offset, raw, event.raw_hash, event.event_timestamp))
            if typ == "user" and not any(block.get("type") == "tool_result" for block in blocks):
                current = AgentTurn(
                    id=_id("turn", f"{session.id}:{len(turns)}"),
                    agent=self.agent,
                    agent_session_id=session.id,
                    turn_index=len(turns),
                    turn_id=str(raw.get("uuid") or "") or None,
                    status="running",
                    started_at=event.event_timestamp,
                    input_tokens=0,
                    cached_input_tokens=0,
                    cache_creation_input_tokens=0,
                    output_tokens=0,
                    reasoning_output_tokens=0,
                    total_tokens=0,
                    outcome="running",
                )
                db.add(current)
                turns.append(current)
            if typ == "summary":
                text = _text(raw.get("summary") or raw.get("content"))
                if text:
                    db.add(CodexMessage(id=_id("msg", str(event.id)), agent_session_id=session.id, event_id=event.id, turn_id=current.id if current else None, role="system", channel="commentary", text=text, markdown_text=text, copy_text=text, sequence=sequence, timestamp=event.event_timestamp))
                    sequence += 1
            for block in blocks:
                kind = block.get("type")
                if kind == "text":
                    text = _text(block.get("text"))
                    if text and current:
                        role = "user" if typ == "user" else "assistant"
                        db.add(CodexMessage(id=_id("msg", f"{event.id}:{sequence}"), agent_session_id=session.id, event_id=event.id, turn_id=current.id, role=role, channel="final", text=text, markdown_text=text, copy_text=text, sequence=sequence, timestamp=event.event_timestamp))
                        sequence += 1
                elif kind == "thinking" and current:
                    text = _text(block.get("thinking") or block.get("text"))
                    if text:
                        db.add(CodexMessage(id=_id("msg", f"{event.id}:{sequence}"), agent_session_id=session.id, event_id=event.id, turn_id=current.id, role="assistant", channel="reasoning", text=text, markdown_text=text, copy_text=text, sequence=sequence, timestamp=event.event_timestamp, is_visible=False))
                        sequence += 1
                elif kind == "tool_use":
                    call_id = str(block.get("id") or f"{event.id}:{sequence}")
                    if call_id in tools:
                        continue
                    tool = ToolCall(id=_id("tool", f"{session.id}:{call_id}"), agent=self.agent, agent_session_id=session.id, turn_id=current.id if current else None, event_id=event.id, call_id=call_id, tool_name=str(block.get("name") or "unknown")[:255], tool_kind="tool", arguments_json=block.get("input") if isinstance(block.get("input"), dict) else None, command_text=_text(block.get("input")), started_at=event.event_timestamp, status="pending")
                    db.add(tool)
                    tools[call_id] = tool
                elif kind == "tool_result":
                    call_id = str(block.get("tool_use_id") or "")
                    tool = tools.get(call_id)
                    if tool:
                        tool.output_text = _text(block.get("content") or block.get("output"))
                        tool.completed_at = event.event_timestamp
                        tool.status = "failed" if block.get("is_error") else "ok"
            if typ == "assistant" and current:
                usage = _usage(raw.get("message") if isinstance(raw.get("message"), dict) else {})
                current.input_tokens += usage.get("input_tokens", 0)
                current.cached_input_tokens += usage.get("cached_input_tokens", 0)
                if hasattr(current, "cache_creation_input_tokens"):
                    current.cache_creation_input_tokens += usage.get("cache_creation_input_tokens", 0)
                current.output_tokens += usage.get("output_tokens", 0)
                current.reasoning_output_tokens += usage.get("reasoning_output_tokens", 0)
                current.total_tokens += usage.get("total_tokens", 0)
        session.end_time = max(
            (e.event_timestamp for e in events if e.event_timestamp),
            default=session.end_time,
        )
        session.last_activity_at = session.end_time or session.start_time
        for turn in turns:
            turn.status = "completed"
            turn.outcome = "completed"
            turn.completed_at = turn.completed_at or session.end_time
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
                        duration_ms=(
                            int((turn.completed_at - turn.started_at).total_seconds() * 1000)
                            if turn.completed_at
                            else None
                        ),
                        confidence="derived",
                        metadata_json={"agent": self.agent},
                    )
                )
        for tool in tools.values():
            if tool.started_at is not None:
                db.add(
                    ActivitySpan(
                        id=_id("span", f"tool:{tool.id}"),
                        agent=self.agent,
                        source_kind=self.kind,
                        source_record_id=f"tool:{tool.call_id}",
                        agent_session_id=session.id,
                        thread_id=session.thread_id,
                        turn_id=tool.turn_id,
                        span_type="tool",
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
                        metadata_json={"agent": self.agent},
                    )
                )
        session.turn_count = len(turns)
        session.input_tokens = sum(t.input_tokens for t in turns)
        session.cached_input_tokens = sum(t.cached_input_tokens for t in turns)
        if hasattr(session, "cache_creation_input_tokens"):
            session.cache_creation_input_tokens = sum(getattr(t, "cache_creation_input_tokens", 0) for t in turns)
        session.output_tokens = sum(t.output_tokens for t in turns)
        session.total_tokens = sum(t.total_tokens for t in turns)
        session.is_ongoing = False
        await db.flush()


__all__ = ["ClaudeJsonlSource"]
