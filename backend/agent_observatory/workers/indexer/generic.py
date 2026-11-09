from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_observatory.db.models import AgentEvent, AgentSession, CodexMessage

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


def _pg_safe(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "\\u0000")
    if isinstance(value, dict):
        return {str(_pg_safe(key)): _pg_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_pg_safe(item) for item in value]
    return value


class GenericJsonlSource:
    kind = "generic_jsonl"

    def __init__(self, root: str | Path, *, agent: str = "generic", parser_version: str = "1"):
        self.root = Path(root).expanduser()
        self.agent = agent
        self.parser_version = parser_version

    @staticmethod
    def identity(path: Path) -> str:
        return "generic:" + hashlib.sha256(str(path.resolve()).encode()).hexdigest()

    def discover(self) -> list[SourceDescriptor]:
        if not self.root.exists():
            return []
        return [
            SourceDescriptor(
                kind=self.kind, identity=self.identity(p), path=p, format="jsonl", agent=self.agent
            )
            for p in sorted(self.root.rglob("*.jsonl"))
            if p.is_file()
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

    def _iter(self, path):
        with path.open("rb") as fh:
            while True:
                raw = fh.readline(8 * 1024 * 1024 + 1)
                if not raw:
                    return
                if len(raw) > 8 * 1024 * 1024 and not raw.endswith(b"\n"):
                    while raw and not raw.endswith(b"\n"):
                        raw = fh.readline(8 * 1024 * 1024 + 1)
                    continue
                if raw.endswith(b"\n"):
                    yield raw[:-1]

    async def ingest(self, db: AsyncSession, plan: SourcePlan) -> AgentSession:
        src = plan.source
        sid = _id("session", src.identity)
        session = await db.scalar(
            select(AgentSession).where(AgentSession.source_identity == src.identity)
        )
        if session is None:
            session = AgentSession(
                id=sid,
                thread_id=src.path.stem,
                rollout_path=str(src.path),
                rollout_real_path=str(src.path.resolve()),
                rollout_format="jsonl",
                agent=self.agent,
                source_kind=self.kind,
                source_identity=src.identity,
                source_path=str(src.path),
                source_format="jsonl",
                parser_version=self.parser_version,
            )
            db.add(session)
            await db.flush()
        else:
            await db.execute(
                delete(CodexMessage).where(CodexMessage.agent_session_id == session.id)
            )
            await db.execute(delete(AgentEvent).where(AgentEvent.agent_session_id == session.id))
        first_user = None
        seq = 0
        start = end = None
        for ln, line in enumerate(self._iter(src.path)):
            try:
                obj = json.loads(line)
                known = isinstance(obj, dict)
            except Exception:
                obj = {
                    "type": "parse_error",
                    "raw": line.decode("utf-8", "replace"),
                    "error": "invalid_json",
                }
                known = False
            if not isinstance(obj, dict):
                obj = {"type": "raw", "value": obj}
                known = False
            obj = _pg_safe(obj)
            typ = str(obj.get("type") or obj.get("event") or "unknown")
            ts = _ts(obj.get("timestamp") or obj.get("time") or obj.get("created_at"))
            ev = AgentEvent(
                agent_session_id=session.id,
                event_index=seq,
                line_number=ln,
                byte_offset=0,
                event_timestamp=ts,
                top_level_type=typ,
                payload_type=typ,
                normalized_type=typ,
                raw_json=obj,
                raw_hash=hashlib.sha256(line).hexdigest(),
                is_known=known,
                parser_version=self.parser_version,
            )
            db.add(ev)
            await db.flush()
            start = start or ts
            end = ts or end
            role = obj.get("role")
            text = obj.get("text") or obj.get("content") or obj.get("message")
            if role in ("user", "assistant") and text is not None:
                if isinstance(text, list):
                    text = "".join(
                        str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in text
                    )
                text = str(text)
                first_user = first_user or (text if role == "user" else None)
                db.add(
                    CodexMessage(
                        id=_id("msg", f"{session.id}:{seq}"),
                        agent_session_id=session.id,
                        event_id=ev.id,
                        role=role,
                        channel="message",
                        text=text,
                        copy_text=text,
                        markdown_text=text,
                        sequence=seq,
                        timestamp=ts,
                    )
                )
            seq += 1
        session.display_title = first_user or session.display_title or session.thread_id
        session.ai_title = session.ai_title or session.display_title
        session.start_time = start
        session.end_time = end
        session.last_activity_at = end or start
        session.file_size = src.path.stat().st_size
        session.file_mtime_ns = src.path.stat().st_mtime_ns
        session.last_indexed_line = seq
        session.index_status = "indexed"
        await db.flush()
        return session
