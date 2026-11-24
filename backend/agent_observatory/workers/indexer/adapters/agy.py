"""Antigravity CLI (``agy``) conversation adapter.

agy keeps one directory per conversation below
``~/.gemini/antigravity-cli/brain/<conversation-id>``.  Next to scratch files
and tool output copies it writes ``.system_generated/logs/transcript_full.jsonl``:
an append-only log with one JSON record per trajectory step (the same steps agy
stores as protobuf in ``conversations/<id>.db``)::

    USER_INPUT        user prompt wrapped in <USER_REQUEST>; starts a turn
    PLANNER_RESPONSE  model reply: thinking, content, tool_calls, token usage
    GENERIC           result of one tool call, written in call order
    SYSTEM_MESSAGE    reminders and background-task notifications

``GENERIC`` steps carry no call id: agy writes one result step per tool call
of the preceding ``PLANNER_RESPONSE``, in the order the calls were issued, so
results are paired with calls positionally.  ``transcript.jsonl`` is the same
log with long fields truncated and is only used when the full log is missing.

The transcript has no title or workspace.  Those come from agy's own
conversation index ``conversation_summaries.db`` (the list ``/resume`` shows).
It is opened with ``immutable=1`` while agy is not writing it, so no
``-wal`` / ``-shm`` file is ever created in agy's directory; while a ``-wal``
already exists it is read like every other live SQLite source (``mode=ro``).

Everything is read-only.  New transcript lines are ingested from the last
byte offset and folded into the existing projection, so an active
conversation costs one bounded read per change instead of a rebuild.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

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

from ..fingerprints import sqlite_signature, sqlite_signature_is_racy
from ..tree_cache import cached_resolve, full_scan_requested
from .base import PlanAction, SourceDescriptor, SourcePlan
from .common import (
    MAX_LINE_BYTES,
    ONGOING_SECONDS,
    as_int,
    clear_session_rows,
    duration_ms,
    iter_raw_jsonl_lines,
    parse_ts,
    pg_safe,
    plan_append_jsonl,
    stable_id,
)

#: Preferred first: ``transcript.jsonl`` truncates long fields.
TRANSCRIPT_NAMES = ("transcript_full.jsonl", "transcript.jsonl")
SUMMARIES_DB = "conversation_summaries.db"

_PREFIX_BYTES = 4096
_EVENT_BATCH = 250
_NORMALIZED = {
    "USER_INPUT": "user_message",
    "PLANNER_RESPONSE": "assistant_response",
    "GENERIC": "tool_result",
    "SYSTEM_MESSAGE": "system_message",
}
_TOOL_KINDS = {
    "run_command": "exec",
    "read_file": "read",
    "write_file": "write",
    "edit_file": "patch",
    "search_web": "web_search",
    "update_steps": "plan",
    "manage_task": "task",
    "invoke_subagent": "agent",
    "define_subagent": "agent",
}
_MUTATING_TOOLS = {"write_file", "edit_file"}
_DETECT = {
    "type": re.compile(rb'"type"\s*:\s*"([A-Z_]+)"'),
    "status": re.compile(rb'"status"\s*:\s*"([A-Z_]+)"'),
    "step_index": re.compile(rb'"step_index"\s*:\s*(\d+)'),
}
_USER_REQUEST = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)
_EXIT_CODE = re.compile(r"Command exited with code (-?\d+)")
_COMPLETED_AT = re.compile(r"^Completed At: (\S+)$", re.M)


def _user_text(content: str) -> str:
    """The prompt the user typed, without agy's metadata envelope."""
    match = _USER_REQUEST.search(content)
    return match.group(1) if match else content.strip()


def _call_position(call_id: str) -> int:
    """Position of a call in its ``<step>:<position>`` id."""
    suffix = call_id.rsplit(":", 1)[-1]
    return int(suffix) if suffix.isdigit() else 0


def _uri_path(uri: str) -> str | None:
    parsed = urlparse(uri)
    if parsed.scheme == "file" and parsed.path:
        return unquote(parsed.path)
    return uri if uri.startswith("/") else None


# --- transcript reader --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgyRecord:
    line_number: int
    byte_offset: int
    byte_length: int
    raw_json: dict[str, Any]
    raw_hash: str
    #: ``False`` for lines that are not a JSON object (kept as raw evidence).
    parsed: bool

    @property
    def step_type(self) -> str:
        if self.parsed:
            return str(self.raw_json.get("type") or "unknown")
        detected = self.raw_json.get("detected")
        if isinstance(detected, dict) and detected.get("type"):
            return str(detected["type"])
        return "unknown"


def _detect(prefix: bytes) -> dict[str, Any]:
    """Best-effort step fields of a line that could not be parsed.

    Positional pairing of tool results needs to know that an unreadable line
    was a ``GENERIC`` step; the field order agy writes puts them up front.
    """
    found: dict[str, Any] = {}
    for key, pattern in _DETECT.items():
        match = pattern.search(prefix)
        if match:
            value = match.group(1).decode()
            found[key] = int(value) if key == "step_index" else value
    return found


def _unparsed(reason: str, data: bytes, size: int) -> dict[str, Any]:
    prefix = data[:_PREFIX_BYTES]
    return {
        "parse_error": reason,
        "byte_length": size,
        "raw": prefix.decode("utf-8", "replace"),
        "truncated": size > len(prefix),
        "detected": _detect(prefix),
    }


def iter_records(path: Path, *, offset: int = 0, line_offset: int = 0) -> Iterator[AgyRecord]:
    """Yield transcript records from ``offset`` with bounded memory.

    A final line without a newline is consumed only when it already is a
    complete JSON object; otherwise the writer is mid-line and the next poll
    retries from the same offset.  Lines above :data:`MAX_LINE_BYTES` are
    hashed in chunks and kept as a ``line_too_large`` record.
    """
    line_number = line_offset
    for item in iter_raw_jsonl_lines(
        path,
        offset=offset,
        max_line_bytes=MAX_LINE_BYTES,
        prefix_bytes=_PREFIX_BYTES,
    ):
        if item.oversized_hash is not None:
            if not item.terminated:
                return  # still being written
            line_number += 1
            yield AgyRecord(
                line_number,
                item.byte_offset,
                item.byte_length,
                _unparsed("line_too_large", item.prefix, item.byte_length),
                item.oversized_hash,
                False,
            )
            continue
        assert item.raw is not None
        body = item.raw.rstrip(b"\r\n")
        if not body.strip():
            continue
        try:
            decoded = json.loads(body)
        except (UnicodeDecodeError, ValueError):
            decoded = None
        if not item.terminated and not isinstance(decoded, dict):
            return  # partial final line
        line_number += 1
        if isinstance(decoded, dict):
            yield AgyRecord(
                line_number,
                item.byte_offset,
                item.byte_length,
                pg_safe(decoded),
                hashlib.sha256(body).hexdigest(),
                True,
            )
        else:
            reason = "invalid_json" if decoded is None else "not_an_object"
            yield AgyRecord(
                line_number,
                item.byte_offset,
                item.byte_length,
                pg_safe(_unparsed(reason, body, len(body))),
                hashlib.sha256(body).hexdigest(),
                False,
            )


# --- conversation metadata ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgyConversationMeta:
    title: str | None = None
    workspace: str | None = None
    parent_id: str | None = None

    def digest(self) -> str:
        payload = json.dumps([self.title, self.workspace, self.parent_id])
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


_NO_META = AgyConversationMeta()


class AgyMetadataIndex:
    """Title / workspace / parent per conversation from ``conversation_summaries.db``.

    Re-read only when the store's signature (main file, ``-wal``, header
    counter) changes; a signature inside the racy window is re-checked at
    most once a second so planning many conversations reads it once.
    """

    TTL = 3600.0
    RACY_RECHECK = 1.0
    _COLUMNS = ("conversation_id", "title", "workspace_uris", "parent_conversation_id")

    def __init__(self, path: Path) -> None:
        self.path = path
        self._signature: tuple[Any, ...] | None = None
        self._racy = False
        self._loaded_at = 0.0
        self._rows: dict[str, AgyConversationMeta] = {}
        self.reads = 0

    def get(self, conversation_id: str | None) -> AgyConversationMeta:
        self._refresh()
        return self._rows.get(conversation_id or "", _NO_META)

    def _refresh(self) -> None:
        now = time.time()
        try:
            signature = sqlite_signature(self.path)
        except OSError:
            self._rows, self._signature = {}, None
            return
        age = now - self._loaded_at
        if (
            signature == self._signature
            and age < self.TTL
            and (not self._racy or age < self.RACY_RECHECK)
        ):
            return
        rows = self._read(wal=signature[4] is not None)
        try:
            after = sqlite_signature(self.path)
        except OSError:
            return
        if rows is None or after != signature:
            return  # unreadable or written meanwhile: keep the last good rows
        self._rows = rows
        self._signature = signature
        self._racy = sqlite_signature_is_racy(signature, now)
        self._loaded_at = now

    def _read(self, *, wal: bool) -> dict[str, AgyConversationMeta] | None:
        # ``immutable=1`` never creates -wal/-shm; it is only safe while no
        # writer has the database open, which an existing -wal rules out.
        uri = self.path.resolve().as_uri() + ("?mode=ro" if wal else "?mode=ro&immutable=1")
        self.reads += 1
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=1.0)
        except sqlite3.Error:
            return None
        try:
            available = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(conversation_summaries)")
            }
            if "conversation_id" not in available:
                return {}
            columns = [name for name in self._COLUMNS if name in available]
            cursor = connection.execute(
                f"SELECT {', '.join(columns)} FROM conversation_summaries"  # noqa: S608 - fixed names
            )
            rows: dict[str, AgyConversationMeta] = {}
            while batch := cursor.fetchmany(500):
                for values in batch:
                    record = dict(zip(columns, values, strict=True))
                    rows[str(record["conversation_id"])] = self._meta(record)
            return rows
        except sqlite3.Error:
            return None
        finally:
            connection.close()

    @staticmethod
    def _meta(record: dict[str, Any]) -> AgyConversationMeta:
        workspace = None
        try:
            uris = json.loads(record.get("workspace_uris") or "[]")
        except (TypeError, ValueError):
            uris = []
        if isinstance(uris, list):
            for uri in uris:
                if isinstance(uri, str) and (workspace := _uri_path(uri)):
                    break
        title = record.get("title")
        parent = record.get("parent_conversation_id")
        return AgyConversationMeta(
            title=title.strip() or None if isinstance(title, str) else None,
            workspace=workspace,
            parent_id=parent.strip() or None if isinstance(parent, str) else None,
        )


# --- adapter ------------------------------------------------------------------


@dataclass(slots=True)
class _State:
    """Projection cursor; rebuilt from PostgreSQL for incremental appends."""

    event_index: int = 0
    sequence: int = 0
    turn: AgentTurn | None = None
    turn_count: int = 0
    pending: list[ToolCall] = field(default_factory=list)
    last_ts: datetime | None = None
    first_user: str | None = None
    turns: dict[str, AgentTurn] = field(default_factory=dict)
    tools: dict[str, ToolCall] = field(default_factory=dict)


class AgyTranscriptSource:
    """Discover and project agy conversations below ``<agy home>/brain``."""

    kind = "agy_jsonl"
    agent = "agy"

    #: Discovery re-lists ``brain`` at least this often even if its mtime
    #: is unchanged.
    RELIST_INTERVAL = 600.0
    RACY_WINDOW = 2.0

    def __init__(self, root: str | Path, *, parser_version: str = "1") -> None:
        #: agy home, e.g. ``~/.gemini/antigravity-cli``
        self.root = Path(root).expanduser()
        self.parser_version = parser_version
        self.metadata = AgyMetadataIndex(self.root / SUMMARIES_DB)
        self._listing: list[str] | None = None
        self._listing_mtime: int | None = None
        self._listing_racy = False
        self._listed_at = 0.0
        self._transcripts: dict[str, Path] = {}

    @property
    def brain_root(self) -> Path:
        return self.root / "brain"

    @staticmethod
    def identity(conversation_dir: Path) -> str:
        canonical = str(cached_resolve(conversation_dir))
        return "agy:" + hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def transcript_for(conversation_dir: Path) -> Path | None:
        logs = conversation_dir / ".system_generated" / "logs"
        for name in TRANSCRIPT_NAMES:
            candidate = logs / name
            if candidate.is_file():
                return candidate
        return None

    # -- discovery ------------------------------------------------------------

    def discover(self) -> list[SourceDescriptor]:
        """List conversation directories, re-listing only when ``brain`` changed.

        A new conversation creates a directory (the ``brain`` mtime moves);
        its transcript appears a moment later, so directories without one are
        re-probed every poll.  Known transcripts are re-verified on full
        cycles; the orchestrator stats them on every poll anyway.
        """
        brain = self.brain_root
        now = time.time()
        try:
            stat = os.stat(brain)
        except OSError:
            self._listing = None
            self._transcripts.clear()
            return []
        if (
            self._listing is None
            or stat.st_mtime_ns != self._listing_mtime
            or self._listing_racy
            or now - self._listed_at >= self.RELIST_INTERVAL
        ):
            try:
                with os.scandir(brain) as entries:
                    names = sorted(
                        entry.name
                        for entry in entries
                        if not entry.name.startswith(".")
                        and entry.is_dir(follow_symlinks=False)
                    )
            except OSError:
                return []
            self._listing = names
            self._listing_mtime = stat.st_mtime_ns
            self._listing_racy = abs(now - stat.st_mtime_ns / 1e9) < self.RACY_WINDOW
            self._listed_at = now
            for gone in set(self._transcripts) - set(names):
                del self._transcripts[gone]
        verify = full_scan_requested()
        sources: list[SourceDescriptor] = []
        for name in self._listing:
            transcript = self._transcripts.get(name)
            if transcript is None or verify:
                transcript = self.transcript_for(brain / name)
                if transcript is None:
                    self._transcripts.pop(name, None)
                    continue
                self._transcripts[name] = transcript
            sources.append(
                SourceDescriptor(
                    kind=self.kind,
                    identity=self.identity(brain / name),
                    path=transcript,
                    format="jsonl",
                    agent=self.agent,
                    thread_id=name,
                )
            )
        return sources

    def watch_paths(self, source: SourceDescriptor) -> tuple[Path, ...]:
        """Every file whose change can alter ``plan`` (never ``-shm``)."""
        store = self.root / SUMMARIES_DB
        return (source.path, store, Path(f"{store}-wal"))

    # -- planning -------------------------------------------------------------

    def plan(self, source: SourceDescriptor, checkpoint: Any | None) -> SourcePlan:
        return plan_append_jsonl(
            source,
            checkpoint,
            self.parser_version,
            extra_fp={"meta": self.metadata.get(source.thread_id).digest()},
            check_path=True,
            check_prefix=True,
            rebuild_on_same_size=False,
            cached_path=True,
        )

    # -- ingestion ------------------------------------------------------------

    async def ingest(self, db: AsyncSession, plan: SourcePlan) -> AgentSession:
        source = plan.source
        conversation_id = source.thread_id or source.path.parents[2].name
        real_path = str(source.path.resolve())
        session = await db.scalar(
            select(AgentSession).where(AgentSession.source_identity == source.identity)
        )
        if session is None:
            session = AgentSession(
                id=stable_id("session", source.identity),
                thread_id=conversation_id,
                agent=self.agent,
                source_kind=self.kind,
                source_identity=source.identity,
                source_format="jsonl",
                rollout_format="jsonl",
                parser_version=self.parser_version,
                turn_count=0,
                total_tokens=0,
                input_tokens=0,
                cached_input_tokens=0,
                cache_creation_input_tokens=0,
                output_tokens=0,
                reasoning_output_tokens=0,
                last_indexed_offset=0,
                last_indexed_line=0,
            )
            db.add(session)
        session.thread_id = conversation_id
        session.rollout_path = str(source.path)
        session.rollout_real_path = f"agy:{real_path}"
        session.source_path = real_path
        session.parser_version = self.parser_version
        await db.flush()

        append = (
            plan.action == PlanAction.APPEND
            and int(session.last_indexed_offset or 0) <= source.path.stat().st_size
        )
        if append:
            state = await self._load_state(db, session)
        else:
            await self._clear(db, session)
            state = _State()

        batch: list[AgyRecord] = []
        for record in iter_records(
            source.path,
            offset=int(session.last_indexed_offset or 0),
            line_offset=int(session.last_indexed_line or 0),
        ):
            batch.append(record)
            if len(batch) >= _EVENT_BATCH:
                await self._ingest_batch(db, session, state, batch)
                batch = []
        if batch:
            await self._ingest_batch(db, session, state, batch)

        meta = self.metadata.get(conversation_id)
        if meta.title:
            session.display_title = meta.title[:255]
            session.ai_title = meta.title[:255]
        elif state.first_user and (
            not append or session.display_title in (None, "", conversation_id)
        ):
            session.display_title = " ".join(state.first_user.split())[:255]
        if not session.display_title:
            session.display_title = conversation_id[:255]
        session.cwd = meta.workspace or session.cwd
        if meta.parent_id and meta.parent_id != conversation_id:
            session.parent_session_id = meta.parent_id
            session.is_subagent = True

        self._write_spans(db, session, state)
        stat = source.path.stat()
        session.file_size = stat.st_size
        session.file_mtime_ns = stat.st_mtime_ns
        session.turn_count = state.turn_count
        session.last_activity_at = state.last_ts or session.start_time
        session.end_time = state.last_ts
        last_turn = state.turn
        session.is_ongoing = bool(
            last_turn is not None
            and last_turn.status == "running"
            and state.last_ts is not None
            and (datetime.now(UTC) - state.last_ts).total_seconds() < ONGOING_SECONDS
        )
        session.index_status = "indexed"
        session.last_error = None
        await db.flush()
        return session

    async def _ingest_batch(
        self,
        db: AsyncSession,
        session: AgentSession,
        state: _State,
        records: list[AgyRecord],
    ) -> None:
        events: list[AgentEvent] = []
        for record in records:
            step_type = record.step_type
            normalized = _NORMALIZED.get(step_type) if record.parsed else None
            source_label = record.raw_json.get("source") if record.parsed else None
            events.append(
                AgentEvent(
                    agent_session_id=session.id,
                    event_index=state.event_index,
                    line_number=record.line_number,
                    byte_offset=record.byte_offset,
                    event_timestamp=parse_ts(record.raw_json.get("created_at")),
                    top_level_type=step_type[:64],
                    payload_type=str(source_label or record.raw_json.get("parse_error") or step_type)[:128],
                    normalized_type=(normalized or ("parse_error" if not record.parsed else "unknown")),
                    raw_json=record.raw_json,
                    raw_hash=record.raw_hash,
                    is_known=normalized is not None,
                    parser_version=self.parser_version,
                )
            )
            state.event_index += 1
        db.add_all(events)
        await db.flush()
        for record, event in zip(records, events, strict=True):
            self._project(db, session, state, record, event)
            session.last_indexed_offset = record.byte_offset + record.byte_length
            session.last_indexed_line = record.line_number
        await db.flush()

    def _project(
        self,
        db: AsyncSession,
        session: AgentSession,
        state: _State,
        record: AgyRecord,
        event: AgentEvent,
    ) -> None:
        raw = record.raw_json
        step_type = record.step_type
        ts = event.event_timestamp
        if ts is not None:
            if session.start_time is None:
                session.start_time = ts
            if state.last_ts is None or ts >= state.last_ts:
                state.last_ts = ts
        content = raw.get("content") if isinstance(raw.get("content"), str) else ""

        if step_type == "USER_INPUT" and record.parsed:
            self._cancel_pending(state)
            self._close_turn(state)
            turn = AgentTurn(
                id=stable_id("turn", f"{session.id}:{state.turn_count}"),
                agent=self.agent,
                agent_session_id=session.id,
                turn_index=state.turn_count,
                turn_id=f"{session.thread_id}:{raw.get('step_index', event.event_index)}"[:255],
                status="running",
                outcome="running",
                started_at=ts,
                input_tokens=0,
                cached_input_tokens=0,
                cache_creation_input_tokens=0,
                output_tokens=0,
                reasoning_output_tokens=0,
                total_tokens=0,
            )
            db.add(turn)
            state.turn = turn
            state.turn_count += 1
            state.turns[turn.id] = turn
            text = _user_text(content)
            if text:
                state.first_user = state.first_user or text
                self._message(db, session, state, event, role="user", channel="final", text=text, part=0)
            return

        if step_type == "PLANNER_RESPONSE" and record.parsed:
            self._cancel_pending(state)
            turn = state.turn or self._implicit_turn(db, session, state, ts)
            state.turns[turn.id] = turn
            model = raw.get("model")
            if isinstance(model, str) and model:
                session.model = model[:255]
            input_tokens = as_int(raw.get("input_tokens"))
            cached = as_int(raw.get("cache_read_tokens"))
            output_tokens = as_int(raw.get("output_tokens"))
            for owner in (turn, session):
                owner.input_tokens = (owner.input_tokens or 0) + input_tokens
                owner.cached_input_tokens = (owner.cached_input_tokens or 0) + cached
                owner.output_tokens = (owner.output_tokens or 0) + output_tokens
                owner.total_tokens = (owner.total_tokens or 0) + input_tokens + cached + output_tokens
            thinking = raw.get("thinking")
            if isinstance(thinking, str) and thinking.strip():
                self._message(
                    db, session, state, event,
                    role="assistant", channel="reasoning", text=thinking, part=0, visible=False,
                )
            calls = raw.get("tool_calls") if isinstance(raw.get("tool_calls"), list) else []
            if content.strip():
                self._message(
                    db, session, state, event,
                    role="assistant", channel="commentary" if calls else "final", text=content, part=1,
                )
            for position, call in enumerate(calls):
                call = call if isinstance(call, dict) else {"value": call}
                name = str(call.get("name") or "unknown")
                args = call.get("args")
                arguments = args if isinstance(args, dict) else ({"value": args} if args is not None else None)
                tool = ToolCall(
                    id=stable_id("tool", f"{session.id}:{event.event_index}:{position}"),
                    agent=self.agent,
                    agent_session_id=session.id,
                    turn_id=turn.id,
                    event_id=event.id,
                    call_id=f"{raw.get('step_index', event.event_index)}:{position}",
                    tool_name=name[:255],
                    tool_kind=_TOOL_KINDS.get(name, "tool"),
                    arguments_json=arguments,
                    command_text=self._command_text(name, arguments),
                    started_at=ts,
                    status="pending",
                    is_mutating=name in _MUTATING_TOOLS,
                )
                db.add(tool)
                state.pending.append(tool)
                state.tools[tool.id] = tool
            if calls:
                turn.status = turn.outcome = "running"
                turn.completed_at = None
                turn.duration_ms = None
            else:
                turn.status = turn.outcome = "completed"
                turn.completed_at = ts
                turn.duration_ms = duration_ms(turn.started_at, ts)
            return

        if step_type == "GENERIC":
            if state.pending:
                tool = state.pending.pop(0)
            else:
                # A result without a preceding call (or a call list we could
                # not read): keep the output visible as its own tool row.
                tool = ToolCall(
                    id=stable_id("tool", f"{session.id}:{event.event_index}:result"),
                    agent=self.agent,
                    agent_session_id=session.id,
                    turn_id=state.turn.id if state.turn else None,
                    event_id=event.id,
                    call_id=f"{raw.get('step_index', event.event_index)}:result",
                    tool_name="unknown",
                    tool_kind="tool",
                    started_at=ts,
                    status="pending",
                )
                db.add(tool)
            state.tools[tool.id] = tool
            status = str(raw.get("status") or (raw.get("detected") or {}).get("status") or "")
            if record.parsed:
                tool.output_text = content
            else:
                tool.output_text = (
                    f"[agy transcript line could not be parsed: {raw.get('parse_error')}, "
                    f"{raw.get('byte_length')} bytes; see the raw event]"
                )
            exit_match = _EXIT_CODE.search(content)
            tool.exit_code = int(exit_match.group(1)) if exit_match else None
            if isinstance(raw.get("error"), str) and raw["error"] and not content:
                tool.output_text = raw["error"]
            if status == "RUNNING":
                tool.status = "running"
                tool.completed_at = None
                return
            completed = _COMPLETED_AT.findall(content)
            tool.completed_at = (parse_ts(completed[-1]) if completed else None) or ts
            failed = status == "ERROR" or bool(raw.get("error")) or (tool.exit_code not in (None, 0))
            tool.status = "failed" if failed else ("ok" if status in {"DONE", ""} else status.lower()[:32])
            return

        if step_type == "SYSTEM_MESSAGE" and record.parsed and content.strip():
            self._message(
                db, session, state, event,
                role="system", channel="commentary", text=content, part=0, system=True,
            )

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _command_text(name: str, arguments: dict[str, Any] | None) -> str | None:
        if not arguments:
            return None
        if name == "run_command" and isinstance(arguments.get("CommandLine"), str):
            return arguments["CommandLine"]
        return json.dumps(arguments, ensure_ascii=False, sort_keys=True)

    def _message(
        self,
        db: AsyncSession,
        session: AgentSession,
        state: _State,
        event: AgentEvent,
        *,
        role: str,
        channel: str,
        text: str,
        part: int,
        visible: bool = True,
        system: bool = False,
    ) -> None:
        db.add(
            CodexMessage(
                id=stable_id("msg", f"{session.id}:{event.event_index}:{part}"),
                agent_session_id=session.id,
                event_id=event.id,
                turn_id=state.turn.id if state.turn else None,
                role=role,
                channel=channel,
                text=text,
                markdown_text=text,
                copy_text=text,
                sequence=state.sequence,
                timestamp=event.event_timestamp,
                is_visible=visible,
                is_system_generated=system,
            )
        )
        state.sequence += 1

    def _implicit_turn(
        self, db: AsyncSession, session: AgentSession, state: _State, ts: datetime | None
    ) -> AgentTurn:
        """A turn for model steps that precede any user prompt."""
        turn = AgentTurn(
            id=stable_id("turn", f"{session.id}:{state.turn_count}"),
            agent=self.agent,
            agent_session_id=session.id,
            turn_index=state.turn_count,
            turn_id=f"{session.thread_id}:implicit:{state.turn_count}"[:255],
            status="running",
            outcome="running",
            started_at=ts,
            input_tokens=0,
            cached_input_tokens=0,
            cache_creation_input_tokens=0,
            output_tokens=0,
            reasoning_output_tokens=0,
            total_tokens=0,
        )
        db.add(turn)
        state.turn = turn
        state.turn_count += 1
        return turn

    @staticmethod
    def _cancel_pending(state: _State) -> None:
        """Calls whose results never arrived before the next step began."""
        for tool in state.pending:
            tool.status = "cancelled"
            state.tools[tool.id] = tool
        state.pending.clear()

    def _close_turn(self, state: _State) -> None:
        turn = state.turn
        if turn is None or turn.status != "running":
            return
        turn.status = turn.outcome = "interrupted"
        turn.completed_at = state.last_ts
        turn.duration_ms = duration_ms(turn.started_at, state.last_ts)
        state.turns[turn.id] = turn

    def _write_spans(self, db: AsyncSession, session: AgentSession, state: _State) -> None:
        for turn in state.turns.values():
            if turn.started_at is None:
                continue
            db.add(
                ActivitySpan(
                    id=stable_id("span", f"turn:{turn.id}"),
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
                    duration_ms=duration_ms(turn.started_at, turn.completed_at),
                    confidence="derived",
                    metadata_json={"agent": self.agent},
                )
            )
        turn_ids = {turn.id: turn.turn_id for turn in state.turns.values()}
        for tool in state.tools.values():
            if tool.started_at is None:
                continue
            db.add(
                ActivitySpan(
                    id=stable_id("span", f"tool:{tool.id}"),
                    agent=self.agent,
                    source_kind=self.kind,
                    source_record_id=f"tool:{tool.call_id}",
                    agent_session_id=session.id,
                    thread_id=session.thread_id,
                    turn_id=turn_ids.get(tool.turn_id or ""),
                    span_type="tool",
                    name=tool.tool_name,
                    status=tool.status,
                    started_at=tool.started_at,
                    completed_at=tool.completed_at,
                    duration_ms=duration_ms(tool.started_at, tool.completed_at),
                    confidence="derived",
                    metadata_json={"agent": self.agent},
                )
            )

    async def _clear(self, db: AsyncSession, session: AgentSession) -> None:
        await clear_session_rows(db, session.id)
        session.last_indexed_offset = 0
        session.last_indexed_line = 0
        session.turn_count = 0
        session.total_tokens = 0
        session.input_tokens = 0
        session.cached_input_tokens = 0
        session.cache_creation_input_tokens = 0
        session.output_tokens = 0
        session.reasoning_output_tokens = 0
        session.start_time = None
        session.end_time = None
        session.model = None

    async def _load_state(self, db: AsyncSession, session: AgentSession) -> _State:
        """Projection state at the stored offset, read back from PostgreSQL."""
        max_event = await db.scalar(
            select(func.max(AgentEvent.event_index)).where(AgentEvent.agent_session_id == session.id)
        )
        max_sequence = await db.scalar(
            select(func.max(CodexMessage.sequence)).where(CodexMessage.agent_session_id == session.id)
        )
        turn = await db.scalar(
            select(AgentTurn)
            .where(AgentTurn.agent_session_id == session.id)
            .order_by(AgentTurn.turn_index.desc())
            .limit(1)
        )
        pending = list(
            (
                await db.scalars(
                    select(ToolCall).where(
                        ToolCall.agent_session_id == session.id,
                        ToolCall.status == "pending",
                    )
                )
            ).all()
        )
        if pending:
            # Only the newest call batch can still receive results.
            newest = max(tool.event_id or 0 for tool in pending)
            pending = sorted(
                (tool for tool in pending if (tool.event_id or 0) == newest),
                key=lambda tool: _call_position(tool.call_id),
            )
        state = _State(
            event_index=int(max_event) + 1 if max_event is not None else 0,
            sequence=int(max_sequence) + 1 if max_sequence is not None else 0,
            turn=turn,
            turn_count=(turn.turn_index + 1) if turn is not None else 0,
            pending=pending,
            last_ts=session.end_time or session.last_activity_at,
        )
        if turn is not None:
            state.turns[turn.id] = turn
        for tool in pending:
            state.tools[tool.id] = tool
        # Spans of rows this pass may change are rewritten at the end.
        span_ids = [stable_id("span", f"turn:{key}") for key in state.turns] + [
            stable_id("span", f"tool:{key}") for key in state.tools
        ]
        if span_ids:
            await db.execute(delete(ActivitySpan).where(ActivitySpan.id.in_(span_ids)))
        return state


__all__ = [
    "AgyConversationMeta",
    "AgyMetadataIndex",
    "AgyRecord",
    "AgyTranscriptSource",
    "iter_records",
]
