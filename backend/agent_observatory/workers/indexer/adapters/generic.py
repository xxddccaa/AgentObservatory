from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_observatory.db.models import AgentEvent, AgentSession, CodexMessage

from ..tree_cache import cached_resolve, rglob_cached
from .base import SourceDescriptor, SourcePlan
from .common import (
    MAX_LINE_BYTES,
    clear_session_rows,
    parse_ts,
    pg_safe,
    plan_stat_rebuild,
    stable_id,
)


class GenericJsonlSource:
    kind = "generic_jsonl"

    def __init__(self, root: str | Path, *, agent: str = "generic", parser_version: str = "1"):
        self.root = Path(root).expanduser()
        self.agent = agent
        self.parser_version = parser_version

    @staticmethod
    def identity(path: Path) -> str:
        return "generic:" + hashlib.sha256(str(cached_resolve(path)).encode()).hexdigest()

    def discover(self) -> list[SourceDescriptor]:
        if not self.root.exists():
            return []
        return [
            SourceDescriptor(
                kind=self.kind, identity=self.identity(p), path=p, format="jsonl", agent=self.agent
            )
            for p in sorted(rglob_cached(self, self.root, "*.jsonl"))
            if p.is_file()
        ]

    def plan(self, source: SourceDescriptor, checkpoint: Any | None = None) -> SourcePlan:
        return plan_stat_rebuild(source, checkpoint, self.parser_version)

    def _iter(self, path):
        with path.open("rb") as fh:
            while True:
                raw = fh.readline(MAX_LINE_BYTES + 1)
                if not raw:
                    return
                if len(raw) > MAX_LINE_BYTES and not raw.endswith(b"\n"):
                    while raw and not raw.endswith(b"\n"):
                        raw = fh.readline(MAX_LINE_BYTES + 1)
                    continue
                if raw.endswith(b"\n"):
                    yield raw[:-1]

    async def ingest(self, db: AsyncSession, plan: SourcePlan) -> AgentSession:
        src = plan.source
        sid = stable_id("session", src.identity)
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
            await clear_session_rows(db, session.id)
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
            obj = pg_safe(obj)
            typ = str(obj.get("type") or obj.get("event") or "unknown")
            ts = parse_ts(obj.get("timestamp") or obj.get("time") or obj.get("created_at"))
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
                        id=stable_id("msg", f"{session.id}:{seq}"),
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
