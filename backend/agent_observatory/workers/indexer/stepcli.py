"""Step CLI native session adapter.

Step CLI stores one directory per session below ``~/.step-cli/sessions``.
The directory contains a resumable ``session.json`` snapshot and an
append-only ``events.jsonl`` stream.  This adapter indexes the stream while
using the snapshot for session metadata and as a fallback transcript source.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
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
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(
                value / 1000 if value > 1e11 else value, tz=UTC
            )
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for part in (_text(item) for item in value) if part)
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        if "content" in value:
            return _text(value["content"])
        if "message" in value:
            return _text(value["message"])
    return str(value)


def _safe_json(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {"value": value}


class StepCliNativeSource:
    """Discover and project Step CLI ``.step-cli/sessions`` directories."""

    kind = "stepcli_json"
    agent = "stepcli"
    parser_version = "1"

    def __init__(self, root: str | Path, *, parser_version: str = "1") -> None:
        self.root = Path(root).expanduser()
        self.parser_version = parser_version

    @staticmethod
    def identity(session_dir: Path) -> str:
        return "stepcli:" + hashlib.sha256(
            str(session_dir.resolve()).encode()
        ).hexdigest()

    def discover(self) -> list[SourceDescriptor]:
        if not self.root.exists() or not self.root.is_dir():
            return []
        result: list[SourceDescriptor] = []
        for session_json in sorted(self.root.glob("*/session.json")):
            if not session_json.is_file():
                continue
            session_id = session_json.parent.name
            result.append(
                SourceDescriptor(
                    kind=self.kind,
                    identity=self.identity(session_json.parent),
                    path=session_json,
                    format="json",
                    agent=self.agent,
                    thread_id=session_id,
                )
            )
        return result

    def plan(
        self, source: SourceDescriptor, checkpoint: Any | None
    ) -> SourcePlan:
        stat = source.path.stat()
        events_path = source.path.with_name("events.jsonl")
        events_stat = events_path.stat() if events_path.is_file() else None
        fingerprint = {
            "path": str(source.path.resolve()),
            "events_path": str(events_path.resolve()),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "events_size": events_stat.st_size if events_stat else 0,
            "events_mtime_ns": events_stat.st_mtime_ns if events_stat else 0,
            "session_id": source.thread_id,
        }
        if checkpoint is None or checkpoint.parser_version != self.parser_version:
            return SourcePlan(source, PlanAction.REBUILD, fingerprint)
        previous = checkpoint.fingerprint_json or {}
        if (
            checkpoint.status == "indexed"
            and all(previous.get(key) == value for key, value in fingerprint.items())
        ):
            return SourcePlan(source, PlanAction.UNCHANGED, fingerprint)
        return SourcePlan(source, PlanAction.REBUILD, fingerprint)

    @staticmethod
    def _iter_events(path: Path) -> Iterator[tuple[int, int, dict[str, Any], bytes]]:
        if not path.is_file():
            return
        with path.open("rb") as handle:
            line_number = 0
            while raw := handle.readline(8 * 1024 * 1024 + 1):
                offset = handle.tell() - len(raw)
                line_number += 1
                if len(raw) > 8 * 1024 * 1024 and not raw.endswith(b"\n"):
                    while raw and not raw.endswith(b"\n"):
                        raw = handle.readline(8 * 1024 * 1024 + 1)
                    obj: dict[str, Any] = {
                        "kind": "parse_error",
                        "error": "line_too_large",
                    }
                    yield line_number, offset, obj, raw
                    continue
                body = raw.rstrip(b"\r\n")
                try:
                    decoded = json.loads(body)
                    obj = _safe_json(decoded)
                except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
                    obj = {
                        "kind": "parse_error",
                        "error": "invalid_json",
                        "raw": body.decode("utf-8", "replace"),
                    }
                yield line_number, offset, obj, body

    @staticmethod
    def _snapshot(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            return {}

    async def ingest(self, db: AsyncSession, plan: SourcePlan) -> AgentSession:
        source = plan.source
        session_dir = source.path.parent
        snapshot = self._snapshot(source.path)
        session_id = source.thread_id or session_dir.name
        runtime = snapshot.get("runtime")
        runtime = runtime if isinstance(runtime, dict) else {}
        workspace = snapshot.get("workspaceRoot")
        model = snapshot.get("model")
        provider = snapshot.get("provider")
        saved_at = _ts(snapshot.get("savedAt"))
        session = await db.scalar(
            select(AgentSession).where(
                AgentSession.source_identity == source.identity
            )
        )
        if session is None:
            session = AgentSession(
                id=_id("session", source.identity),
                thread_id=session_id,
                rollout_path=str(session_dir),
                rollout_real_path=f"stepcli:{session_dir.resolve()}",
                rollout_format="json",
                agent=self.agent,
                source_kind=self.kind,
                source_identity=source.identity,
                source_path=str(session_dir.resolve()),
                source_format="json",
                parser_version=self.parser_version,
            )
            db.add(session)
            await db.flush()
        else:
            session.thread_id = session_id
            session.rollout_path = str(session_dir)
            session.rollout_real_path = f"stepcli:{session_dir.resolve()}"
            session.source_path = str(session_dir.resolve())
            session.agent = self.agent

        await self._clear(db, session.id)
        session.turn_count = 0
        session.total_tokens = 0
        session.input_tokens = 0
        session.output_tokens = 0
        session.cwd = workspace if isinstance(workspace, str) else session.cwd
        session.model = model if isinstance(model, str) else session.model
        session.originator = provider if isinstance(provider, str) else session.originator
        session.start_time = session.start_time or saved_at
        session.last_activity_at = saved_at

        events_path = session_dir / "events.jsonl"
        messages: list[tuple[str, str, datetime | None, int | None]] = []
        event_count = 0
        last_timestamp: datetime | None = None
        for line_number, offset, raw, raw_bytes in self._iter_events(events_path):
            event_count += 1
            kind = str(raw.get("kind") or raw.get("type") or "unknown")
            payload = raw.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            timestamp = _ts(raw.get("recordedAt") or raw.get("timestamp") or raw.get("ts"))
            last_timestamp = timestamp or last_timestamp
            event = AgentEvent(
                agent_session_id=session.id,
                event_index=event_count - 1,
                line_number=line_number,
                byte_offset=offset,
                event_timestamp=timestamp,
                top_level_type=kind,
                payload_type=str(payload.get("type") or kind),
                normalized_type=(
                    "message"
                    if kind == "session.message"
                    else "verifier"
                    if kind == "session.verifier.verdict"
                    else kind
                ),
                raw_json=raw,
                raw_hash=hashlib.sha256(raw_bytes).hexdigest(),
                is_known=kind in {"session.message", "session.verifier.verdict"},
                parser_version=self.parser_version,
            )
            db.add(event)
            await db.flush()
            message = payload.get("message")
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "")
            if role not in {"user", "assistant", "system"}:
                continue
            text = _text(message.get("content"))
            if text:
                messages.append((role, text, timestamp, event.id))

        # Very old/native sessions may have a snapshot but no event stream.
        if not messages:
            memory = snapshot.get("memory")
            memory_messages = memory.get("messages") if isinstance(memory, dict) else []
            if isinstance(memory_messages, list):
                for item in memory_messages:
                    if not isinstance(item, dict):
                        continue
                    role = str(item.get("role") or "")
                    text = _text(item.get("content"))
                    if role in {"user", "assistant", "system"} and text:
                        messages.append((role, text, saved_at, None))

        current_turn: AgentTurn | None = None
        first_user: str | None = None
        for sequence, (role, text, timestamp, event_id) in enumerate(messages):
            if role == "user":
                turn_index = session.turn_count or 0
                turn_id = _id("turn", f"{session.id}:{turn_index}")
                current_turn = AgentTurn(
                    id=turn_id,
                    agent=self.agent,
                    agent_session_id=session.id,
                    turn_index=turn_index,
                    turn_id=f"{session_id}:{turn_index}",
                    status="running",
                    started_at=timestamp,
                    outcome="running",
                )
                db.add(current_turn)
                session.turn_count = turn_index + 1
                first_user = first_user or text
            message = CodexMessage(
                id=_id("message", f"{session.id}:{sequence}"),
                agent_session_id=session.id,
                event_id=event_id,
                turn_id=current_turn.id if current_turn else None,
                role=role,
                channel="message",
                text=text,
                markdown_text=text,
                copy_text=text,
                sequence=sequence,
                timestamp=timestamp,
            )
            db.add(message)
            if role == "assistant" and current_turn is not None:
                current_turn.status = "completed"
                current_turn.outcome = "completed"
                current_turn.completed_at = timestamp

        if first_user:
            session.display_title = first_user[:255]
            session.ai_title = session.ai_title or session.display_title
        elif not session.display_title:
            session.display_title = session_id[:255]
            session.ai_title = session.display_title
        session.turn_count = session.turn_count or 0
        session.end_time = last_timestamp or saved_at
        session.last_activity_at = last_timestamp or saved_at
        session.is_ongoing = bool(current_turn and current_turn.completed_at is None)
        session.file_size = source.path.stat().st_size
        session.file_mtime_ns = source.path.stat().st_mtime_ns
        session.last_indexed_line = event_count
        session.last_indexed_offset = events_path.stat().st_size if events_path.is_file() else 0
        session.index_status = "indexed"
        await db.flush()
        return session

    @staticmethod
    async def _clear(db: AsyncSession, session_id: str) -> None:
        for model in (ActivitySpan, CodexMessage, ToolCall, AgentTurn, AgentEvent):
            await db.execute(delete(model).where(model.agent_session_id == session_id))


__all__ = ["StepCliNativeSource"]
