"""Read-only Codex rollout scanner with bounded incremental reads."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import zstandard as zstd
from sqlalchemy import delete, insert, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_observatory.db.models import (
    AgentEvent,
    AgentSession,
    AgentTurn,
    CodexMessage,
    ToolCall,
)
from agent_observatory.workers.indexer.parser import NormalizedEvent, parse_json_line

logger = logging.getLogger(__name__)


class RolloutScanner:
    def __init__(
        self,
        sessions_root: str | Path,
        *,
        parser_version: str = "4",
        agent: str = "codex",
        source_kind: str = "codex_jsonl",
        identity_prefix: str = "codex",
    ) -> None:
        self.root = Path(sessions_root).expanduser()
        self.parser_version = parser_version
        self.agent = agent
        self.source_kind = source_kind
        self.identity_prefix = identity_prefix
        #: Rolling projection counters, published through the worker heartbeat
        #: so the cost of the structured rebuild stays visible.
        self.stats: dict[str, float] = {
            "projections": 0,
            "skipped": 0,
            "projection_ms": 0.0,
            "projection_ms_max": 0.0,
        }

    def discover(self) -> list[Path]:
        if not self.root.exists():
            return []
        paths = [
            path
            for path in self.root.rglob("rollout-*.jsonl*")
            if path.is_file() and path.name.endswith((".jsonl", ".jsonl.zst"))
        ]
        return sorted(
            (
                path for path in paths
                if not (path.name.endswith(".jsonl.zst") and path.with_suffix("").exists())
            ),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )

    @staticmethod
    def source_identity(path: Path, prefix: str = "codex") -> str:
        """Return one stable identity for plain and compressed siblings."""
        canonical = path.with_suffix("") if path.name.endswith(".zst") else path
        canonical = canonical.resolve()
        if prefix == "xdcode":
            for parent in canonical.parents:
                if parent.name in {"sessions", "archived_sessions"}:
                    canonical = parent.parent / "sessions" / canonical.name
                    break
        digest = hashlib.sha256(str(canonical).encode()).hexdigest()
        return f"{prefix}:{digest}"

    def iter_lines(
        self, path: Path, offset: int = 0, line_offset: int = 0
    ) -> Iterator[tuple[int, int, str]]:
        """Yield ``(line_number, byte_offset, text)`` for each rollout line.

        For plain ``.jsonl`` files ``offset`` is a byte position we ``seek`` to,
        so resuming is O(new bytes). For ``.jsonl.zst`` files ``offset`` is a
        line count: zstd streams are not randomly seekable, so we must
        decompress from the start and skip already-indexed lines (see
        :meth:`index_path` for the logged limitation).
        """
        if path.name.endswith(".zst"):
            with path.open("rb") as compressed_handle:
                reader = zstd.ZstdDecompressor().stream_reader(compressed_handle)
                line_number = 0
                pending = b""
                while True:
                    chunk = reader.read(1024 * 1024)
                    if not chunk:
                        break
                    pending += chunk
                    while b"\n" in pending:
                        raw_line, pending = pending.split(b"\n", 1)
                        line_number += 1
                        if line_number > offset:
                            yield line_number, 0, raw_line.decode("utf-8")
                if pending:
                    line_number += 1
                    if line_number > offset:
                        yield line_number, 0, pending.decode("utf-8")
            return
        with path.open("rb") as handle:
            handle.seek(offset)
            line_number = line_offset
            while True:
                byte_offset = handle.tell()
                raw_bytes = handle.readline()
                if not raw_bytes or not raw_bytes.endswith(b"\n"):
                    break
                line_number += 1
                yield line_number, byte_offset, raw_bytes.decode("utf-8")

    async def index_path(
        self, db: AsyncSession, path: Path, *, force_rebuild: bool = False
    ) -> AgentSession:
        stat = path.stat()
        is_zst = path.name.endswith(".zst")
        real_path = str(path.resolve())
        source_identity = self.source_identity(path, self.identity_prefix)
        sibling_real_path = (
            str(path.with_suffix("").resolve())
            if is_zst
            else str(path.with_name(path.name + ".zst").resolve())
        )
        session = await db.scalar(
            select(AgentSession).where(
                or_(
                    AgentSession.source_identity == source_identity,
                    AgentSession.rollout_real_path == real_path,
                    AgentSession.rollout_real_path == sibling_real_path,
                )
            )
        )
        if session is None:
            session = AgentSession(
                id=os.urandom(12).hex(),
                thread_id=path.stem,
                rollout_path=str(path),
                rollout_real_path=real_path,
                rollout_format="jsonl",
                rollout_compression="zstd" if is_zst else None,
                agent=self.agent,
                source_kind=self.source_kind,
                source_identity=source_identity,
                source_path=real_path,
                source_format="jsonl",
                source_compression="zstd" if is_zst else None,
                parser_version=self.parser_version,
            )
            db.add(session)
            await db.flush()
        elif session.parser_version != self.parser_version:
            await self._reset_session_index(db, session)
            session.last_indexed_offset = 0
            session.last_indexed_line = 0
            session.file_size = 0
            session.file_mtime_ns = 0
            session.file_sha256 = None
            session.parser_version = self.parser_version
            session.index_status = "discovered"
            session.last_error = None
            await db.flush()
        elif session.rollout_real_path != real_path:
            session.rollout_path = str(path)
            session.rollout_real_path = real_path
            session.rollout_compression = "zstd" if is_zst else None
            session.last_indexed_offset = 0
            session.last_indexed_line = 0
            session.file_size = 0
            session.file_mtime_ns = 0
            await self._reset_session_index(db, session)
            await db.flush()
        session.agent = self.agent
        session.source_kind = self.source_kind
        session.source_identity = source_identity
        session.source_path = real_path
        session.source_format = "jsonl"
        session.source_compression = "zstd" if is_zst else None

        # Truncation / rewrite detection. For .jsonl the persisted byte offset is
        # authoritative; for .zst (line-based resume) compare the compressed
        # size against the previously recorded size. On a shrink we drop the
        # stale events and reset every derived counter so re-indexing rebuilds
        # cleanly instead of double-counting turns/tokens.
        rewritten_same_size = (
            session.file_size > 0
            and stat.st_size == session.file_size
            and stat.st_mtime_ns != session.file_mtime_ns
            and session.file_sha256 is not None
            and self._sha256(path) != session.file_sha256
        )
        truncated = (force_rebuild and session.file_size > 0) or rewritten_same_size or (
            stat.st_size < session.file_size if is_zst else stat.st_size < session.last_indexed_offset
        )
        if truncated:
            await self._reset_session_index(db, session)
            session.last_indexed_offset = 0
            session.last_indexed_line = 0
            session.turn_count = 0
            session.input_tokens = 0
            session.cached_input_tokens = 0
            session.cache_creation_input_tokens = 0
            session.output_tokens = 0
            session.reasoning_output_tokens = 0
            session.total_tokens = 0
            session.index_status = "discovered"
            await db.flush()

        session.index_status = "indexing"
        session.last_error = None
        if is_zst and session.last_indexed_line:
            logger.info(
                "zst resume for %s: decompressing full stream to skip %d indexed lines "
                "(zstd streams are not seekable)",
                path.name,
                session.last_indexed_line,
            )

        event_index = session.last_indexed_line
        event_batch: list[dict[str, Any]] = []
        line_iterator = (
            self.iter_lines(path, offset=session.last_indexed_line)
            if is_zst
            else self.iter_lines(
                path, offset=session.last_indexed_offset, line_offset=session.last_indexed_line
            )
        )
        for line_number, byte_offset, line in line_iterator:
            if not line.strip():
                continue
            try:
                event = parse_json_line(line)
            except (ValueError, json.JSONDecodeError) as exc:
                if not is_zst and not line.endswith(("\n", "\r")):
                    # Codex may be in the middle of appending the final JSON
                    # object. Leave the offset at the last complete line and
                    # retry on the next poll.
                    break
                session.index_status = "parse_error"
                session.last_error = str(exc)[:500]
                continue
            event_batch.append(
                {
                    "agent_session_id": session.id,
                    "event_index": event_index,
                    "line_number": line_number,
                    "byte_offset": byte_offset,
                    "event_timestamp": event.timestamp,
                    "top_level_type": event.top_level_type,
                    "payload_type": event.payload_type,
                    "normalized_type": event.normalized_type,
                    "raw_json": event.raw_json,
                    "raw_hash": event.raw_hash,
                    "is_known": event.is_known,
                    "parser_version": self.parser_version,
                }
            )
            if len(event_batch) >= 250:
                await db.execute(insert(AgentEvent), event_batch)
                event_batch.clear()
            self._apply_metadata(session, event)
            event_index += 1
            session.last_indexed_line = line_number
            if not is_zst:
                session.last_indexed_offset = byte_offset + len(line.encode("utf-8"))

        if event_batch:
            await db.execute(insert(AgentEvent), event_batch)
        final_stat = path.stat()
        session.file_size = final_stat.st_size
        session.file_mtime_ns = final_stat.st_mtime_ns
        session.file_sha256 = self._sha256(path)
        if session.index_status != "parse_error":
            session.index_status = "indexed"
        await db.flush()

        # Rebuild the structured projection (messages/tools/turns + session
        # aggregates + ongoing/ended status) during indexing, not lazily at read
        # time. Imported locally to avoid any import cycle with the service layer.
        from agent_observatory.services.history import project_events

        projection_started = time.perf_counter()
        rebuilt = await project_events(db, session)
        elapsed_ms = (time.perf_counter() - projection_started) * 1000
        self.stats["projection_ms"] = round(self.stats["projection_ms"] + elapsed_ms, 1)
        self.stats["projection_ms_max"] = round(
            max(self.stats["projection_ms_max"], elapsed_ms), 1
        )
        self.stats["projections"] += 1
        if not rebuilt:
            self.stats["skipped"] += 1
        return session

    @staticmethod
    async def _reset_session_index(db: AsyncSession, session: AgentSession) -> None:
        """Drop a session's events and projection, invalidating its cursor.

        ``projected_event_index`` is the projection fast path's only trust
        signal: rewriting events under an unchanged cursor would make a stale
        projection look current, so the cursor is reset here, atomically with
        the deletes.
        """
        session.projected_event_index = -1
        for model in (CodexMessage, ToolCall, AgentTurn, AgentEvent):
            await db.execute(
                delete(model)
                .where(model.agent_session_id == session.id)
                .execution_options(synchronize_session=False)
            )

    @staticmethod
    def _apply_metadata(session: AgentSession, event: NormalizedEvent) -> None:
        """Fold session-scoped metadata out of a rollout event.

        Turn counts and token totals are intentionally *not* accumulated here;
        :func:`agent_observatory.services.history.project_events` recomputes them
        from the persisted events as the single source of truth.
        """
        raw = event.raw_json
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
        if not isinstance(payload, dict):
            return
        if event.top_level_type in {"session_meta", "session_meta_root"}:
            raw_thread = payload.get("thread")
            thread: dict[str, Any] = raw_thread if isinstance(raw_thread, dict) else {}
            current_id = payload.get("id") or thread.get("sessionId") or payload.get("thread_id")
            parent_id = payload.get("session_id")
            session.thread_id = str(current_id or parent_id or session.thread_id)
            if parent_id and str(parent_id) != str(current_id or ""):
                session.parent_session_id = str(parent_id)
                session.is_subagent = True
            session.cwd = payload.get("cwd")
            session.originator = payload.get("originator")
            session.cli_version = payload.get("cli_version")
            session.model = payload.get("model") or payload.get("model_provider") or session.model
            session.git_branch = (payload.get("git") or {}).get("branch")
            session.ai_title = payload.get("ai-title")
            session.approval_mode = payload.get("ask_for_approval")
            session.history_base_thread_id = (payload.get("history_base") or {}).get("thread_id")
            payload_timestamp = payload.get("timestamp")
            if isinstance(payload_timestamp, str):
                try:
                    session.start_time = datetime.fromisoformat(
                        payload_timestamp.replace("Z", "+00:00")
                    ).astimezone(UTC)
                except ValueError:
                    session.start_time = event.timestamp
            elif event.timestamp is not None:
                session.start_time = event.timestamp
        elif raw.get("type") == "turn_context" and payload.get("model"):
            # session_meta only carries model_provider; turn_context has the
            # concrete model id (e.g. "gpt-5.5").
            session.model = str(payload["model"])

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
