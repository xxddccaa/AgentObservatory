from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import zstandard as zstd
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


def _ts(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, tz=UTC)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _id(prefix: str, value: str) -> str:
    return hashlib.sha256(f"{prefix}:{value}".encode()).hexdigest()[:48]


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (_text(item) for item in value)))
    if isinstance(value, dict):
        for key in ("text", "content", "output"):
            if key in value:
                return _text(value[key])
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _pg_safe(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "\\u0000")
    if isinstance(value, dict):
        return {str(_pg_safe(key)): _pg_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_pg_safe(item) for item in value]
    return value


def _turn_key(data: dict[str, Any], fallback: str) -> str:
    turn = data.get("turn")
    if isinstance(turn, dict):
        return str(turn.get("id") or turn.get("turn_id") or fallback)
    if turn is not None:
        return str(turn)
    return str(data.get("turn_id") or data.get("id") or fallback)


def _token(usage: dict[str, Any], *names: str) -> int:
    for name in names:
        try:
            if usage.get(name) is not None:
                return int(usage[name])
        except (TypeError, ValueError):
            continue
    return 0


class DshJsonlSource:
    kind = "dsh_jsonl"
    agent = "dsh"

    def __init__(self, root: str | Path, parser_version: str = "1"):
        self.root = Path(root).expanduser()
        self.parser_version = parser_version

    @staticmethod
    def identity(path: Path) -> str:
        return "dsh:" + hashlib.sha256(str(path.resolve()).encode()).hexdigest()

    def discover(self) -> list[SourceDescriptor]:
        if not self.root.exists():
            return []
        return [
            SourceDescriptor(
                kind=self.kind,
                identity=self.identity(p),
                path=p,
                format="jsonl",
                agent=self.agent,
                compression="zstd",
            )
            for p in sorted(self.root.rglob("session.jsonl.zstd"))
            if p.is_file()
        ]

    def plan(self, source: SourceDescriptor, checkpoint: Any | None = None) -> SourcePlan:
        st = source.path.stat()
        fp = {
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
            "path": str(source.path.resolve()),
            "compression": "zstd",
        }
        if checkpoint is None or getattr(checkpoint, "parser_version", None) != self.parser_version:
            return SourcePlan(source, PlanAction.REBUILD, fp)
        prev = getattr(checkpoint, "fingerprint_json", {}) or {}
        if (
            getattr(checkpoint, "status", None) == "indexed"
            and prev.get("size") == st.st_size
            and prev.get("mtime_ns") == st.st_mtime_ns
        ):
            return SourcePlan(source, PlanAction.UNCHANGED, fp)
        # Zstandard frames are streamed from the beginning; changed files are
        # rebuilt rather than pretending byte offsets are seekable.
        return SourcePlan(source, PlanAction.REBUILD, fp)

    def _iter_lines(self, path):
        with path.open("rb") as fh, zstd.ZstdDecompressor().stream_reader(fh) as rd:
            buf = b""
            while chunk := rd.read(65536):
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if len(line) <= 8 * 1024 * 1024:
                        yield line
                    else:
                        yield b'{"type":"parse_error","error":"line_too_large"}'
                if len(buf) > 8 * 1024 * 1024:
                    buf = b""
            if buf.strip() and len(buf) <= 8 * 1024 * 1024:
                yield buf

    async def ingest(self, db: AsyncSession, plan: SourcePlan) -> AgentSession:
        src = plan.source
        session = await db.scalar(
            select(AgentSession).where(AgentSession.source_identity == src.identity)
        )
        sid = _id("session", src.identity)
        if session is None:
            session = AgentSession(
                id=sid,
                thread_id=src.identity,
                rollout_path=str(src.path),
                rollout_real_path=str(src.path.resolve()),
                rollout_format="jsonl",
                rollout_compression="zstd",
                agent=self.agent,
                source_kind=self.kind,
                source_identity=src.identity,
                source_path=str(src.path),
                source_format="jsonl",
                source_compression="zstd",
                parser_version=self.parser_version,
            )
            db.add(session)
            await db.flush()
        else:
            for model in (
                ActivitySpan,
                CodexMessage,
                ToolCall,
                AgentTurn,
                AgentEvent,
            ):
                await db.execute(delete(model).where(model.agent_session_id == session.id))
        session.agent = self.agent
        session.source_kind = self.kind
        session.source_compression = "zstd"
        session.input_tokens = 0
        session.cached_input_tokens = 0
        session.cache_creation_input_tokens = 0
        session.output_tokens = 0
        session.reasoning_output_tokens = 0
        turns: dict[str, AgentTurn] = {}
        tools: dict[str, ToolCall] = {}
        messages: list[CodexMessage] = []
        first_user: str | None = None
        idx = 0
        start = end = None
        tokens = 0
        for ln, line in enumerate(self._iter_lines(src.path)):
            try:
                obj = json.loads(line)
            except Exception:
                obj = {"type": "parse_error", "raw": line.decode("utf-8", "replace")}
            if not isinstance(obj, dict):
                obj = {"type": "raw", "value": obj}
            obj = _pg_safe(obj)
            typ = str(obj.get("type", "unknown"))
            data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
            # DSH records often use generic ``data.turn``/``data.step`` envelopes.
            if typ in {"turn", "step"}:
                phase = str(data.get("phase") or data.get("status") or data.get("event") or "start")
                typ = f"turn/{'end' if phase in {'end', 'complete', 'completed', 'finish'} else 'start'}"
            elif typ == "message":
                role = str(data.get("role") or obj.get("role") or "assistant")
                typ = f"{role}/message"
            elif typ in {"call", "tool"}:
                typ = "tool/call"
            elif typ in {"result", "tool_result"}:
                typ = "tool/result"
            if ln == 0 and typ == "session":
                session.thread_id = str(obj.get("id") or src.identity)
                session.cwd = obj.get("cwd")
                session.parent_session_id = obj.get("parentSession")
                session.start_time = _ts(obj.get("createdAt"))
                session.is_subagent = bool(obj.get("parentSession"))
                start = session.start_time
            ts = _ts(obj.get("time") or obj.get("timestamp"))
            ev = AgentEvent(
                agent_session_id=session.id,
                event_index=idx,
                line_number=ln,
                byte_offset=0,
                event_timestamp=ts,
                top_level_type=typ,
                payload_type=typ,
                normalized_type=typ,
                raw_json=obj,
                raw_hash=hashlib.sha256(line).hexdigest(),
                is_known=typ
                in {
                    "session",
                    "turn/start",
                    "turn/end",
                    "user/message",
                    "assistant/message",
                    "tool/call",
                    "tool/result",
                    "session/title",
                },
                parser_version=self.parser_version,
            )
            db.add(ev)
            await db.flush()
            idx += 1
            start = start or ts
            end = ts or end
            if typ == "session/title":
                session.thread_name = str(data.get("title") or "")[:255]
                session.display_title = session.thread_name
            elif typ == "request/context":
                model = data.get("model")
                if isinstance(model, str) and model:
                    session.model = model
            elif typ == "user/message":
                text = _text(
                    data.get("text")
                    or data.get("message")
                    or data.get("content")
                )
                source_info = (
                    data.get("source")
                    if isinstance(data.get("source"), dict)
                    else {}
                )
                if source_info.get("kind", "user") == "user":
                    first_user = first_user or text
                messages.append(
                    CodexMessage(
                        id=_id("msg", f"{session.id}:{idx}"),
                        agent_session_id=session.id,
                        event_id=ev.id,
                        role="user",
                        channel="final",
                        text=text,
                        copy_text=text,
                        markdown_text=text,
                        sequence=len(messages),
                        timestamp=ts,
                    )
                )
            elif typ == "assistant/message":
                message = data.get("message")
                message_data = message if isinstance(message, dict) else data
                content = message_data.get("content")
                blocks = content if isinstance(content, list) else [content]
                for block in blocks:
                    if block is None:
                        continue
                    block_type = (
                        str(block.get("type") or "text")
                        if isinstance(block, dict)
                        else "text"
                    )
                    if block_type not in {
                        "text",
                        "reasoning",
                        "thinking",
                        "redacted_thinking",
                    }:
                        continue
                    text = _text(block)
                    if not text:
                        continue
                    channel = (
                        "reasoning"
                        if block_type
                        in {"reasoning", "thinking", "redacted_thinking"}
                        else "final"
                    )
                    messages.append(
                        CodexMessage(
                            id=_id(
                                "msg",
                                f"{session.id}:{idx}:{len(messages)}",
                            ),
                            agent_session_id=session.id,
                            event_id=ev.id,
                            role="assistant",
                            channel=channel,
                            text=text,
                            copy_text=text,
                            markdown_text=text,
                            sequence=len(messages),
                            timestamp=ts,
                        )
                    )
                session.model = session.model or message_data.get("model")
                usage = (
                    data.get("usage")
                    or data.get("tokenUsage")
                    or (
                        message.get("usage")
                        if isinstance(message, dict)
                        else {}
                    )
                    or {}
                )
                input_tokens = _token(usage, "input_tokens", "inputTokens", "input")
                cached_tokens = _token(
                    usage, "cached_input_tokens", "cacheReadTokens"
                )
                cache_write_tokens = _token(
                    usage, "cache_creation_input_tokens", "cacheWriteTokens"
                )
                output_tokens = _token(
                    usage, "output_tokens", "outputTokens", "output"
                )
                reasoning_tokens = _token(
                    usage, "reasoning_output_tokens", "reasoningTokens", "reasoning"
                )
                tokens += _token(
                    usage, "total_tokens", "totalTokens", "total"
                ) or (
                    input_tokens
                    + cached_tokens
                    + cache_write_tokens
                    + output_tokens
                )
                session.input_tokens += input_tokens
                session.cached_input_tokens += cached_tokens
                session.cache_creation_input_tokens += cache_write_tokens
                session.output_tokens += output_tokens
                session.reasoning_output_tokens += reasoning_tokens
                turn_id = _turn_key(data, "")
                turn = turns.get(turn_id)
                if turn is None and turns:
                    # Some DSH versions omit turn_id on message envelopes.
                    turn = turns[next(reversed(turns))]
                if turn is not None:
                    turn.input_tokens += input_tokens
                    turn.cached_input_tokens += cached_tokens
                    turn.cache_creation_input_tokens += cache_write_tokens
                    turn.output_tokens += output_tokens
                    turn.reasoning_output_tokens += reasoning_tokens
                    turn.total_tokens += (
                        input_tokens
                        + cached_tokens
                        + cache_write_tokens
                        + output_tokens
                    )
            elif typ == "turn/start":
                tid = _turn_key(data, str(idx))
                turns[tid] = AgentTurn(
                    id=_id("turn", f"{session.id}:{tid}"),
                    agent=self.agent,
                    agent_session_id=session.id,
                    turn_index=len(turns),
                    turn_id=tid,
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
                db.add(turns[tid])
            elif typ == "turn/end":
                turn = turns.get(_turn_key(data, ""))
                if turn is not None:
                    reason = (
                        data.get("reason")
                        if isinstance(data.get("reason"), dict)
                        else {}
                    )
                    outcome = str(reason.get("kind") or data.get("status") or "completed")
                    turn.completed_at = ts
                    turn.status = outcome
                    turn.outcome = (
                        "completed"
                        if outcome == "completed"
                        else "interrupted"
                        if outcome in {"aborted", "interrupted"}
                        else "failed"
                        if outcome in {"error", "blocked"}
                        else outcome
                    )
                    error = (
                        reason.get("error")
                        if isinstance(reason.get("error"), dict)
                        else {}
                    )
                    turn.error_kind = (
                        str(error.get("code"))[:64] if error.get("code") else None
                    )
                    turn.error_message = (
                        str(error.get("message")) if error.get("message") else None
                    )
            elif typ == "tool/call":
                cid = str(data.get("call_id") or data.get("callId") or data.get("id") or idx)
                tid = _turn_key(data, "")
                arguments = data.get("arguments")
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {"raw": arguments}
                tc = ToolCall(
                    id=_id("tool", f"{session.id}:{cid}"),
                    agent=self.agent,
                    agent_session_id=session.id,
                    turn_id=turns.get(tid).id if tid in turns else None,
                    event_id=ev.id,
                    call_id=cid,
                    tool_name=str(data.get("name") or data.get("tool") or data.get("toolName") or "unknown")[:255],
                    tool_kind="tool",
                    arguments_json=(
                        arguments
                        if isinstance(arguments, dict)
                        else data.get("input")
                        if isinstance(data.get("input"), dict)
                        else None
                    ),
                    command_text=(
                        str(data.get("command"))
                        if data.get("command") is not None
                        else None
                    ),
                    started_at=ts,
                    status="pending",
                )
                tools[cid] = tc
                db.add(tc)
            elif typ == "tool/result":
                message = (
                    data.get("message")
                    if isinstance(data.get("message"), dict)
                    else {}
                )
                result_block = next(
                    (
                        block
                        for block in (
                            message.get("content")
                            if isinstance(message.get("content"), list)
                            else []
                        )
                        if isinstance(block, dict)
                        and block.get("type") in {"tool-result", "tool_result"}
                    ),
                    {},
                )
                tool = tools.get(
                    str(
                        data.get("call_id")
                        or data.get("callId")
                        or data.get("id")
                        or result_block.get("toolCallId")
                        or result_block.get("tool_use_id")
                        or ""
                    )
                )
                if tool is not None:
                    tool.output_text = _text(
                        data.get("output")
                        or data.get("result")
                        or result_block.get("content")
                        or message
                    )
                    tool.completed_at = ts
                    tool.status = (
                        "failed"
                        if data.get("error") or result_block.get("isError")
                        else "completed"
                    )
        if messages:
            db.add_all(messages)
        for turn in turns.values():
            if turn.started_at is None:
                continue
            duration_ms = (
                max(
                    0,
                    int(
                        (turn.completed_at - turn.started_at).total_seconds()
                        * 1000
                    ),
                )
                if turn.completed_at
                else None
            )
            turn.duration_ms = duration_ms
            db.add(
                ActivitySpan(
                    id=_id("span", f"turn:{turn.id}"),
                    agent=self.agent,
                    source_kind=self.kind,
                    source_record_id=f"turn:{turn.turn_id}",
                    agent_session_id=session.id,
                    thread_id=session.thread_id,
                    turn_id=turn.turn_id,
                    span_type="turn",
                    name=f"Turn {turn.turn_index + 1}",
                    status=turn.status,
                    started_at=turn.started_at,
                    completed_at=turn.completed_at,
                    duration_ms=duration_ms,
                    confidence="exact",
                    metadata_json={},
                )
            )
        for tool in tools.values():
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
                        max(
                            0,
                            int(
                                (
                                    tool.completed_at - tool.started_at
                                ).total_seconds()
                                * 1000
                            ),
                        )
                        if tool.completed_at
                        else None
                    ),
                    confidence="exact",
                    metadata_json={},
                )
            )
        session.display_title = session.display_title or first_user or session.thread_id
        session.ai_title = session.ai_title or session.display_title
        session.start_time = session.start_time or start
        session.end_time = end
        session.last_activity_at = end or session.start_time
        session.turn_count = len(turns)
        session.total_tokens = tokens
        session.file_size = src.path.stat().st_size
        session.file_mtime_ns = src.path.stat().st_mtime_ns
        session.last_indexed_line = idx
        session.index_status = "indexed"
        session.is_ongoing = any(t.completed_at is None for t in turns.values())
        await db.flush()
        return session
