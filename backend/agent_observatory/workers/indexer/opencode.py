from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
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


def _id(prefix: str, value: str) -> str:
    return hashlib.sha256(f"{prefix}:{value}".encode()).hexdigest()[:48]


def _ts(value: object) -> datetime | None:
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, tz=UTC)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (_text(item) for item in value)))
    if isinstance(value, dict):
        for key in ("text", "content", "output", "value"):
            if key in value:
                return _text(value[key])
    return str(value)


def _int(value: object) -> int:
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _pg_safe(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "\\u0000")
    if isinstance(value, dict):
        return {str(_pg_safe(key)): _pg_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_pg_safe(item) for item in value]
    return value


class OpenCodeJsonSource:
    kind = "opencode_json"
    agent = "opencode"

    def __init__(self, root: str | Path, parser_version: str = "1"):
        self.root = Path(root).expanduser()
        self.parser_version = parser_version

    @property
    def store_root(self) -> Path:
        """Resolve both ``<root>/{session,message}`` and ``<root>/storage/*`` layouts."""
        direct = self.root / "session"
        if direct.exists():
            return self.root
        nested = self.root / "storage"
        return nested if (nested / "session").exists() else self.root

    @staticmethod
    def identity(path: Path) -> str:
        return "opencode:" + hashlib.sha256(str(path.resolve()).encode()).hexdigest()

    def discover(self) -> list[SourceDescriptor]:
        if not self.root.exists():
            return []
        paths = []
        for p in self.root.rglob("*.json"):
            if any(part in {"session", "sessions"} for part in p.parts):
                paths.append(p)
        return [
            SourceDescriptor(
                kind=self.kind, identity=self.identity(p), path=p, format="json", agent=self.agent
            )
            for p in sorted(paths)
        ]

    def plan(self, source: SourceDescriptor, checkpoint: Any | None = None) -> SourcePlan:
        st = source.path.stat()
        fp = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "path": str(source.path.resolve())}
        if checkpoint is None or getattr(checkpoint, "parser_version", None) != self.parser_version:
            return SourcePlan(source, PlanAction.REBUILD, fp)
        prev = getattr(checkpoint, "fingerprint_json", {}) or {}
        if (
            getattr(checkpoint, "status", None) == "indexed"
            and prev.get("size") == st.st_size
            and prev.get("mtime_ns") == st.st_mtime_ns
        ):
            return SourcePlan(source, PlanAction.UNCHANGED, fp)
        return SourcePlan(source, PlanAction.REBUILD, fp)

    async def ingest(self, db: AsyncSession, plan: SourcePlan) -> AgentSession:
        src = plan.source
        try:
            root = json.loads(src.path.read_text())
        except Exception:
            root = {}
        if not isinstance(root, dict):
            root = {"value": root}
        root = _pg_safe(root)
        sid = _id("session", src.identity)
        session = await db.scalar(
            select(AgentSession).where(AgentSession.source_identity == src.identity)
        )
        if session is None:
            session = AgentSession(
                id=sid,
                thread_id=str(root.get("id") or root.get("sessionID") or src.path.stem),
                rollout_path=str(src.path),
                rollout_real_path=str(src.path.resolve()),
                rollout_format="json",
                agent=self.agent,
                source_kind=self.kind,
                source_identity=src.identity,
                source_path=str(src.path),
                source_format="json",
                parser_version=self.parser_version,
            )
            db.add(session)
            await db.flush()
        else:
            for m in (CodexMessage, ToolCall, AgentTurn, AgentEvent):
                await db.execute(delete(m).where(m.agent_session_id == session.id))
        session.cwd = root.get("cwd") or root.get("workingDirectory")
        session.model = root.get("model") or root.get("modelName")
        session.display_title = root.get("title") or root.get("name") or session.thread_id
        records = []
        # associated message files
        msgdir = self.store_root / "message" / session.thread_id
        if not msgdir.exists():
            msgdir = self.store_root / "messages" / session.thread_id
        if msgdir.exists():
            for p in sorted(msgdir.rglob("*.json")):
                try:
                    with p.open("r", encoding="utf-8") as handle:
                        d = json.load(handle)
                    records.append((p, d))
                except Exception:
                    continue
        if not records:
            records = [(src.path, root)]
        seq = 0
        turn_index = -1
        current_turn: AgentTurn | None = None
        turns: list[AgentTurn] = []
        tool_calls: list[ToolCall] = []
        total_tokens = 0
        for _path, obj in records:
            if not isinstance(obj, dict):
                obj = {"value": obj}
            obj = _pg_safe(obj)
            typ = str(obj.get("type") or obj.get("role") or "record")
            ts = _ts(obj.get("timestamp") or obj.get("time"))
            ev = AgentEvent(
                agent_session_id=session.id,
                event_index=seq,
                line_number=seq,
                byte_offset=0,
                event_timestamp=ts,
                top_level_type=typ,
                payload_type=typ,
                normalized_type=typ,
                raw_json=obj,
                raw_hash=hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest(),
                is_known=typ
                in {"user", "assistant", "message", "tool", "tool-call", "tool_result"},
                parser_version=self.parser_version,
            )
            db.add(ev)
            await db.flush()
            role = obj.get("role") or (
                "user" if typ == "user" else "assistant" if typ == "assistant" else None
            )
            if role == "user":
                turn_index += 1
                current_turn = AgentTurn(
                    id=_id("turn", f"{session.id}:{turn_index}"),
                    agent=self.agent,
                    agent_session_id=session.id,
                    turn_index=turn_index,
                    turn_id=str(obj.get("id") or turn_index),
                    status="running",
                    started_at=ts,
                    outcome="running",
                )
                db.add(current_turn)
                turns.append(current_turn)
            text = obj.get("text") or obj.get("content") or obj.get("message")
            parts = obj.get("parts")
            if text is None and isinstance(parts, list):
                text = [part for part in parts if isinstance(part, dict) and part.get("type") in {"text", "reasoning"}]
            if role and isinstance(text, (str, list)):
                if isinstance(text, list):
                    text = "".join(
                        str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in text
                    )
                db.add(
                    CodexMessage(
                        id=_id("msg", f"{session.id}:{seq}"),
                        agent_session_id=session.id,
                        event_id=ev.id,
                        role=str(role),
                        channel="reasoning" if any(
                            isinstance(part, dict) and part.get("type") == "reasoning"
                            for part in (parts or [])
                        ) else "message",
                        text=str(text),
                        copy_text=str(text),
                        markdown_text=str(text),
                        sequence=seq,
                        timestamp=ts,
                    )
                )
            usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
            usage = usage or (obj.get("tokens") if isinstance(obj.get("tokens"), dict) else {})
            in_tokens = _int(usage.get("input") or usage.get("input_tokens"))
            out_tokens = _int(usage.get("output") or usage.get("output_tokens"))
            total_tokens += _int(usage.get("total") or usage.get("total_tokens")) or in_tokens + out_tokens
            if current_turn is not None and role == "assistant":
                current_turn.input_tokens += in_tokens
                current_turn.output_tokens += out_tokens
                current_turn.total_tokens += in_tokens + out_tokens
                current_turn.completed_at = ts
                current_turn.status = "completed"
                current_turn.outcome = "completed"
            tool_parts = parts if isinstance(parts, list) else []
            if typ in {"tool", "tool-call", "tool_use"}:
                tool_parts = [obj]
            for part in tool_parts:
                if not isinstance(part, dict) or part.get("type") not in {"tool", "tool-call", "tool_use", "tool-invocation"}:
                    continue
                state = part.get("state") if isinstance(part.get("state"), dict) else {}
                cid = str(part.get("id") or part.get("callID") or part.get("call_id") or obj.get("id") or seq)
                tool_call = ToolCall(
                        id=_id("tool", f"{session.id}:{cid}"),
                        agent=self.agent,
                        agent_session_id=session.id,
                        event_id=ev.id,
                        call_id=cid,
                        turn_id=current_turn.id if current_turn else None,
                        tool_name=str(part.get("name") or part.get("tool") or part.get("toolName") or "unknown")[:255],
                        tool_kind="tool",
                        arguments_json=part.get("input") if isinstance(part.get("input"), dict)
                        else state.get("input") if isinstance(state.get("input"), dict)
                        else part.get("arguments")
                        if isinstance(part.get("arguments"), dict)
                        else None,
                        output_text=_text(part.get("output") or state.get("output")) or None,
                        status="completed" if (part.get("output") or state.get("output")) else "pending",
                        started_at=ts,
                        completed_at=ts if (part.get("output") or state.get("output")) else None,
                    )
                db.add(tool_call)
                tool_calls.append(tool_call)
            seq += 1
        session.turn_count = max(0, turn_index + 1)
        session.input_tokens = sum(turn.input_tokens or 0 for turn in turns)
        session.output_tokens = sum(turn.output_tokens or 0 for turn in turns)
        session.total_tokens = total_tokens
        for turn in turns:
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
                            _int((turn.completed_at - turn.started_at).total_seconds() * 1000)
                            if turn.completed_at
                            else None
                        ),
                        confidence="derived",
                        metadata_json={},
                    )
                )
        for tool in tool_calls:
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
                            _int((tool.completed_at - tool.started_at).total_seconds() * 1000)
                            if tool.completed_at
                            else None
                        ),
                        confidence="derived",
                        metadata_json={},
                    )
                )
        session.file_size = src.path.stat().st_size
        session.file_mtime_ns = src.path.stat().st_mtime_ns
        session.last_indexed_line = seq
        session.index_status = "indexed"
        await db.flush()
        return session
