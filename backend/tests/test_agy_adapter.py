from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent_observatory.db.models import (
    ActivitySpan,
    AgentEvent,
    AgentSession,
    AgentTurn,
    CodexMessage,
    IndexCheckpoint,
    ToolCall,
)
from agent_observatory.workers.indexer.adapters import PlanAction, SourcePlan
from agent_observatory.workers.indexer.adapters.agy import (
    AgyMetadataIndex,
    AgyTranscriptSource,
    iter_records,
)
from agent_observatory.workers.indexer.adapters.stepcode import StepCodeImporter
from agent_observatory.workers.indexer.orchestrator import IndexerOrchestrator
from agent_observatory.workers.indexer.scanner import RolloutScanner

CONVERSATION = "11111111-2222-4333-8444-555555555555"


def _steps() -> list[dict]:
    """A synthetic conversation in agy's transcript_full.jsonl shape."""
    return [
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": "2026-11-24T06:00:00Z",
            "content": (
                "<USER_REQUEST>\nfix the login bug\n</USER_REQUEST>\n"
                "<ADDITIONAL_METADATA>\nThe current local time is: 2026-11-24T14:00:00+08:00.\n"
                "</ADDITIONAL_METADATA>"
            ),
        },
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-11-24T06:00:02Z",
            "model": "gemini-test-high",
            "input_tokens": 1000,
            "cache_read_tokens": 0,
            "output_tokens": 50,
            "thinking": "Inspect the repository first.",
            "tool_calls": [
                {
                    "name": "run_command",
                    "args": {"CommandLine": "git status", "WaitMsBeforeAsync": 5000},
                },
                {"name": "read_file", "args": {"Path": "/work/app/login.py"}},
            ],
        },
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "GENERIC",
            "status": "DONE",
            "created_at": "2026-11-24T06:00:03Z",
            "content": (
                "Created At: 2026-11-24T14:00:03+08:00\n"
                "Completed At: 2026-11-24T14:00:04+08:00\n"
                "Command exited with code 0.\n\nCommand output:\nclean"
            ),
        },
        {
            "step_index": 3,
            "source": "MODEL",
            "type": "GENERIC",
            "status": "DONE",
            "created_at": "2026-11-24T06:00:03Z",
            "content": "def login():\n    return None\n",
        },
        {
            "step_index": 4,
            "source": "SYSTEM",
            "type": "SYSTEM_MESSAGE",
            "status": "DONE",
            "created_at": "2026-11-24T06:00:05Z",
            "content": "<SYSTEM_MESSAGE>\nReminder: work efficiently.\n</SYSTEM_MESSAGE>",
        },
        {
            "step_index": 5,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-11-24T06:00:06Z",
            "model": "gemini-test-high",
            "input_tokens": 200,
            "cache_read_tokens": 1000,
            "output_tokens": 40,
            "content": "Patching login.",
            "tool_calls": [
                {
                    "name": "edit_file",
                    "args": {"Path": "/work/app/login.py", "Target": "None", "Replacement": "True"},
                },
                {"name": "run_command", "args": {"CommandLine": "pytest -q"}},
            ],
        },
        {
            "step_index": 6,
            "source": "MODEL",
            "type": "GENERIC",
            "status": "ERROR",
            "error": "file does not exist",
            "created_at": "2026-11-24T06:00:07Z",
            "content": "Encountered error in step execution: file does not exist",
        },
        {
            "step_index": 7,
            "source": "MODEL",
            "type": "GENERIC",
            "status": "DONE",
            "created_at": "2026-11-24T06:00:09Z",
            "content": "Command exited with code 1.\n\nCommand output:\n1 failed",
        },
        {
            "step_index": 8,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-11-24T06:00:10Z",
            "model": "gemini-test-high",
            "input_tokens": 300,
            "cache_read_tokens": 1200,
            "output_tokens": 60,
            "content": "Login is fixed; one unrelated test still fails.",
        },
        {
            "step_index": 9,
            "source": "MODEL",
            "type": "FUTURE_STEP_KIND",
            "status": "DONE",
            "created_at": "2026-11-24T06:00:11Z",
            "opaque": {"kept": True},
        },
        {
            "step_index": 10,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": "2026-11-24T06:05:00Z",
            "content": "<USER_REQUEST>\nthanks\n</USER_REQUEST>",
        },
    ]


def _lines(steps: list[dict]) -> str:
    return "".join(json.dumps(step, ensure_ascii=False) + "\n" for step in steps)


def _home(tmp_path: Path, name: str = "agy") -> Path:
    home = tmp_path / name
    (home / "brain").mkdir(parents=True)
    return home


def _transcript(home: Path, conversation: str = CONVERSATION, name: str = "transcript_full.jsonl") -> Path:
    logs = home / "brain" / conversation / ".system_generated" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs / name


def _summaries(home: Path, rows: list[tuple[str, str, str, str]]) -> Path:
    """agy's conversation index, created in WAL mode like the real store."""
    path = home / "conversation_summaries.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS conversation_summaries ("
        "conversation_id text PRIMARY KEY, title text NOT NULL DEFAULT '', "
        "preview text NOT NULL DEFAULT '', step_count integer NOT NULL DEFAULT 0, "
        "workspace_uris text NOT NULL, parent_conversation_id text NOT NULL DEFAULT '', "
        "raw_summary blob)"
    )
    connection.execute("DELETE FROM conversation_summaries")
    connection.executemany(
        "INSERT INTO conversation_summaries "
        "(conversation_id, title, workspace_uris, parent_conversation_id) VALUES (?, ?, ?, ?)",
        rows,
    )
    connection.commit()
    connection.close()  # last connection: checkpoints and removes -wal/-shm
    return path


def _age(*paths: Path, seconds: float = 3600) -> None:
    stamp = time.time() - seconds
    for path in paths:
        os.utime(path, (stamp, stamp))


def _checkpoint(plan, session: AgentSession) -> SimpleNamespace:
    return SimpleNamespace(
        parser_version="1",
        status="indexed",
        fingerprint_json=dict(plan.fingerprint),
        cursor_json={
            "byte_offset": session.last_indexed_offset,
            "line_number": session.last_indexed_line,
        },
    )


async def _projection(db, session_id: str) -> dict:
    messages = (
        await db.scalars(
            select(CodexMessage)
            .where(CodexMessage.agent_session_id == session_id)
            .order_by(CodexMessage.sequence)
        )
    ).all()
    tools = (
        await db.scalars(
            select(ToolCall).where(ToolCall.agent_session_id == session_id).order_by(ToolCall.call_id)
        )
    ).all()
    turns = (
        await db.scalars(
            select(AgentTurn).where(AgentTurn.agent_session_id == session_id).order_by(AgentTurn.turn_index)
        )
    ).all()
    events = (
        await db.scalars(
            select(AgentEvent).where(AgentEvent.agent_session_id == session_id).order_by(AgentEvent.event_index)
        )
    ).all()
    spans = (
        await db.scalars(select(ActivitySpan).where(ActivitySpan.agent_session_id == session_id))
    ).all()
    return {
        "messages": [(m.sequence, m.role, m.channel, m.text, m.is_visible) for m in messages],
        "tools": [
            (t.call_id, t.tool_name, t.tool_kind, t.status, t.exit_code, t.output_text, t.is_mutating)
            for t in tools
        ],
        "turns": [
            (t.turn_index, t.status, t.input_tokens, t.cached_input_tokens, t.output_tokens, t.total_tokens)
            for t in turns
        ],
        "events": [(e.event_index, e.top_level_type, e.is_known) for e in events],
        "spans": sorted((s.span_type, s.name, s.status) for s in spans),
    }


def test_agy_discovers_conversations_and_prefers_full_transcript(tmp_path: Path) -> None:
    home = _home(tmp_path)
    full = _transcript(home)
    full.write_text(_lines(_steps()[:1]))
    _transcript(home, name="transcript.jsonl").write_text(_lines(_steps()[:1]))
    (home / "brain" / "no-logs-yet").mkdir()
    (home / "brain" / ".hidden" / ".system_generated" / "logs").mkdir(parents=True)
    (home / "brain" / ".hidden" / ".system_generated" / "logs" / "transcript_full.jsonl").write_text("{}\n")

    adapter = AgyTranscriptSource(home)
    sources = adapter.discover()

    assert [source.path for source in sources] == [full]
    source = sources[0]
    assert source.agent == "agy"
    assert source.kind == "agy_jsonl"
    assert source.thread_id == CONVERSATION
    assert source.identity.startswith("agy:")
    assert adapter.watch_paths(source)[0] == full

    # A conversation whose log appears after its directory is picked up on
    # the next poll without the brain directory changing again.
    late = _transcript(home, "no-logs-yet", name="transcript.jsonl")
    late.write_text(_lines(_steps()[:1]))
    assert {s.thread_id for s in adapter.discover()} == {CONVERSATION, "no-logs-yet"}


def test_agy_missing_home_discovers_nothing(tmp_path: Path) -> None:
    adapter = AgyTranscriptSource(tmp_path / "absent")
    assert adapter.discover() == []
    (tmp_path / "absent" / "brain").mkdir(parents=True)
    _transcript(tmp_path / "absent").write_text(_lines(_steps()[:1]))
    assert len(adapter.discover()) == 1


def test_agy_reader_keeps_bad_lines_and_waits_for_partial_ones(tmp_path: Path) -> None:
    path = tmp_path / "transcript_full.jsonl"
    good = json.dumps(_steps()[0])
    path.write_bytes(
        (good + "\n").encode()
        + b'{"step_index": 2, "source": "MODEL", "type": "GENERIC", "content": "broken\n'
        + b"[1, 2]\n"
        + b'{"step_index": 9, "type": "USER_INPUT"'
    )
    records = list(iter_records(path))
    assert [r.parsed for r in records] == [True, False, False]
    assert records[1].raw_json["parse_error"] == "invalid_json"
    assert records[1].step_type == "GENERIC"
    assert records[2].raw_json["parse_error"] == "not_an_object"
    consumed = records[-1].byte_offset + records[-1].byte_length
    assert consumed < path.stat().st_size  # the unterminated fragment waits

    # A complete object without its newline yet is a whole record.
    with path.open("ab") as handle:
        handle.write(b', "content": "<USER_REQUEST>hi</USER_REQUEST>"}')
    tail = list(iter_records(path, offset=consumed, line_offset=records[-1].line_number))
    assert len(tail) == 1 and tail[0].parsed
    assert tail[0].byte_offset + tail[0].byte_length == path.stat().st_size


def test_agy_metadata_reader_never_creates_sqlite_side_files(tmp_path: Path) -> None:
    home = _home(tmp_path)
    store = _summaries(
        home,
        [(CONVERSATION, "Fix login", json.dumps(["file:///work/my%20app"]), "")],
    )
    before = sorted(p.name for p in home.iterdir())
    index = AgyMetadataIndex(store)

    meta = index.get(CONVERSATION)
    assert meta.title == "Fix login"
    assert meta.workspace == "/work/my app"
    assert index.get("unknown").title is None
    assert sorted(p.name for p in home.iterdir()) == before

    _age(store)
    reads = index.reads
    index.get(CONVERSATION)
    index.get(CONVERSATION)
    assert index.reads == reads + 1  # racy signature re-read once, then cached
    index.get(CONVERSATION)
    assert index.reads == reads + 1


def test_agy_plan_detects_unchanged_append_rewrite_and_metadata(tmp_path: Path) -> None:
    home = _home(tmp_path)
    path = _transcript(home)
    path.write_text(_lines(_steps()[:3]))
    _summaries(home, [(CONVERSATION, "Fix login", json.dumps(["file:///work/app"]), "")])
    adapter = AgyTranscriptSource(home)
    source = adapter.discover()[0]

    first = adapter.plan(source, None)
    assert first.action is PlanAction.REBUILD
    session = SimpleNamespace(last_indexed_offset=path.stat().st_size, last_indexed_line=3)
    checkpoint = _checkpoint(first, session)
    assert adapter.plan(source, checkpoint).action is PlanAction.UNCHANGED

    with path.open("a") as handle:
        handle.write(_lines(_steps()[3:4]))
    assert adapter.plan(source, checkpoint).action is PlanAction.APPEND

    path.write_text(_lines([{**_steps()[0], "content": "<USER_REQUEST>rewritten</USER_REQUEST>"}] + _steps()[1:4]))
    assert adapter.plan(source, checkpoint).action is PlanAction.REBUILD

    path.write_text(_lines(_steps()[:1]))
    assert adapter.plan(source, checkpoint).action is PlanAction.REBUILD

    path.write_text(_lines(_steps()[:3]))
    fresh = adapter.plan(source, None)
    checkpoint = _checkpoint(fresh, session)
    assert adapter.plan(source, checkpoint).action is PlanAction.UNCHANGED
    _summaries(home, [(CONVERSATION, "Renamed by agy", json.dumps(["file:///work/app"]), "")])
    assert adapter.plan(source, checkpoint).action is PlanAction.APPEND

    stale = SimpleNamespace(**{**vars(checkpoint), "parser_version": "0"})
    assert adapter.plan(source, stale).action is PlanAction.REBUILD


@pytest.mark.anyio
async def test_agy_ingest_projects_messages_tools_tokens_and_metadata(
    tmp_path: Path, db_session
) -> None:
    home = _home(tmp_path)
    path = _transcript(home)
    path.write_bytes(_lines(_steps()).encode() + b"{not json}\n")
    _summaries(
        home,
        [(CONVERSATION, "Fix the login bug", json.dumps(["file:///work/app"]), "parent-conv")],
    )
    adapter = AgyTranscriptSource(home)
    source = adapter.discover()[0]

    session = await adapter.ingest(db_session, adapter.plan(source, None))

    assert session.agent == "agy"
    assert session.source_kind == "agy_jsonl"
    assert session.thread_id == CONVERSATION
    assert session.cwd == "/work/app"
    assert session.display_title == "Fix the login bug"
    assert session.ai_title == "Fix the login bug"
    assert session.parent_session_id == "parent-conv"
    assert session.is_subagent is True
    assert session.model == "gemini-test-high"
    assert session.turn_count == 2
    assert session.input_tokens == 1500
    assert session.cached_input_tokens == 2200
    assert session.output_tokens == 150
    assert session.total_tokens == 3850
    assert session.start_time == datetime(2026, 11, 24, 6, 0, tzinfo=UTC)
    assert session.end_time == datetime(2026, 11, 24, 6, 5, tzinfo=UTC)
    assert session.last_indexed_offset == path.stat().st_size
    assert session.last_indexed_line == 12

    projection = await _projection(db_session, session.id)
    assert projection["messages"] == [
        (0, "user", "final", "fix the login bug", True),
        (1, "assistant", "reasoning", "Inspect the repository first.", False),
        (2, "system", "commentary", "<SYSTEM_MESSAGE>\nReminder: work efficiently.\n</SYSTEM_MESSAGE>", True),
        (3, "assistant", "commentary", "Patching login.", True),
        (4, "assistant", "final", "Login is fixed; one unrelated test still fails.", True),
        (5, "user", "final", "thanks", True),
    ]
    tools = {tool[0]: tool for tool in projection["tools"]}
    assert tools["1:0"][1:5] == ("run_command", "exec", "ok", 0)
    assert tools["1:0"][5].endswith("Command output:\nclean")
    assert tools["1:1"][1:4] == ("read_file", "read", "ok")
    assert tools["1:1"][5] == "def login():\n    return None\n"
    assert tools["5:0"][1:4] == ("edit_file", "patch", "failed")
    assert tools["5:0"][6] is True
    assert tools["5:1"][1:5] == ("run_command", "exec", "failed", 1)
    command = await db_session.scalar(
        select(ToolCall.command_text).where(
            ToolCall.agent_session_id == session.id, ToolCall.call_id == "5:1"
        )
    )
    assert command == "pytest -q"
    completed = await db_session.scalar(
        select(ToolCall.completed_at).where(
            ToolCall.agent_session_id == session.id, ToolCall.call_id == "1:0"
        )
    )
    assert completed == datetime(2026, 11, 24, 6, 0, 4, tzinfo=UTC)

    assert projection["turns"] == [
        (0, "completed", 1500, 2200, 150, 3850),
        (1, "running", 0, 0, 0, 0),
    ]
    assert projection["events"][9] == (9, "FUTURE_STEP_KIND", False)
    assert projection["events"][11] == (11, "unknown", False)
    unknown = await db_session.scalar(
        select(AgentEvent).where(AgentEvent.agent_session_id == session.id, AgentEvent.event_index == 9)
    )
    assert unknown.raw_json["opaque"] == {"kept": True}
    raw_user = await db_session.scalar(
        select(AgentEvent).where(AgentEvent.agent_session_id == session.id, AgentEvent.event_index == 0)
    )
    assert "<ADDITIONAL_METADATA>" in raw_user.raw_json["content"]
    assert ("turn", "Turn 1", "completed") in projection["spans"]
    assert ("tool", "edit_file", "failed") in projection["spans"]


@pytest.mark.anyio
async def test_agy_append_matches_full_rebuild(tmp_path: Path, db_session) -> None:
    steps = _steps()
    incremental_home = _home(tmp_path, "incremental")
    full_home = _home(tmp_path, "full")
    incremental = _transcript(incremental_home)
    _transcript(full_home).write_text(_lines(steps))

    adapter = AgyTranscriptSource(incremental_home)
    # Split inside the second tool batch: one result is already written and
    # the other call is still pending when the first ingest runs.
    incremental.write_text(_lines(steps[:7]))
    source = adapter.discover()[0]
    plan = adapter.plan(source, None)
    session = await adapter.ingest(db_session, plan)
    partial = await _projection(db_session, session.id)
    assert ("5:1", "run_command", "exec", "pending", None, None, False) in partial["tools"]
    assert partial["turns"][0][1] == "running"

    checkpoint = _checkpoint(plan, session)
    with incremental.open("a") as handle:
        handle.write(_lines(steps[7:]))
    append_plan = adapter.plan(source, checkpoint)
    assert append_plan.action is PlanAction.APPEND
    session = await adapter.ingest(db_session, append_plan)

    full_adapter = AgyTranscriptSource(full_home)
    full_source = full_adapter.discover()[0]
    reference = await full_adapter.ingest(db_session, full_adapter.plan(full_source, None))

    assert await _projection(db_session, session.id) == await _projection(db_session, reference.id)
    for field in (
        "turn_count",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "total_tokens",
        "start_time",
        "end_time",
        "display_title",
        "model",
    ):
        assert getattr(session, field) == getattr(reference, field), field
    assert session.display_title == "fix the login bug"

    # Nothing new is UNCHANGED; forcing an append anyway is a projection no-op.
    again = adapter.plan(source, _checkpoint(append_plan, session))
    assert again.action is PlanAction.UNCHANGED
    before = await _projection(db_session, session.id)
    await adapter.ingest(db_session, SourcePlan(source, PlanAction.APPEND, again.fingerprint))
    assert await _projection(db_session, session.id) == before


@pytest.mark.anyio
async def test_agy_rebuild_replaces_previous_rows(tmp_path: Path, db_session) -> None:
    home = _home(tmp_path)
    path = _transcript(home)
    path.write_text(_lines(_steps()))
    adapter = AgyTranscriptSource(home)
    source = adapter.discover()[0]
    session = await adapter.ingest(db_session, adapter.plan(source, None))

    path.write_text(_lines(_steps()[:2]))
    session = await adapter.ingest(db_session, adapter.plan(source, None))
    projection = await _projection(db_session, session.id)
    assert len(projection["events"]) == 2
    assert [tool[3] for tool in projection["tools"]] == ["pending", "pending"]
    assert session.turn_count == 1
    assert session.total_tokens == 1050
    assert session.is_ongoing is False  # synthetic timestamps are long past


@pytest.mark.anyio
async def test_agy_indexes_through_orchestrator_and_settles(tmp_path: Path, db_session) -> None:
    home = _home(tmp_path)
    path = _transcript(home)
    path.write_text(_lines(_steps()[:4]))
    _summaries(home, [(CONVERSATION, "Fix login", json.dumps(["file:///work/app"]), "")])
    _age(path, home / "conversation_summaries.db")
    adapter = AgyTranscriptSource(home)
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    orchestrator = IndexerOrchestrator(
        factory,
        scanner=RolloutScanner(tmp_path / "empty-codex"),
        stepcode=StepCodeImporter(tmp_path / "empty-stepcode"),
        worker_id="indexer-agy",
        started_at=datetime.now(UTC),
        sources=[adapter],
    )

    assert await orchestrator.scan_once() == 1
    assert await orchestrator.scan_once() == 0
    reads = adapter.metadata.reads
    assert await orchestrator.scan_once(full=False) == 0
    assert adapter.metadata.reads == reads  # settled: store not reopened

    with path.open("a") as handle:
        handle.write(_lines(_steps()[4:]))
    _age(path)
    assert await orchestrator.scan_once(full=False) == 1
    async with factory() as check:
        session = await check.scalar(select(AgentSession).where(AgentSession.agent == "agy"))
        assert session is not None
        assert session.turn_count == 2
        assert session.cwd == "/work/app"
        checkpoint = await check.scalar(
            select(IndexCheckpoint).where(IndexCheckpoint.agent_session_id == session.id)
        )
        assert checkpoint is not None
        assert checkpoint.status == "indexed"
        assert checkpoint.cursor_json["byte_offset"] == path.stat().st_size
        events = (
            await check.scalars(select(AgentEvent.event_index).where(AgentEvent.agent_session_id == session.id))
        ).all()
        assert sorted(events) == list(range(len(_steps())))


def test_registry_registers_agy_home_and_extra_roots(tmp_path: Path, monkeypatch) -> None:
    from agent_observatory.config import Settings
    from agent_observatory.workers.indexer.registry import build_registry

    home = _home(tmp_path)
    extra = _home(tmp_path, "agy-extra")
    monkeypatch.setenv("AGENT_OBS_AGY_HOME", str(home))
    monkeypatch.setenv("AGENT_OBS_EXTRA_ROOTS", f"agy:{extra},agy:{home}")
    config = Settings(_env_file=None)
    adapters = [adapter for adapter in build_registry(config) if adapter.agent == "agy"]
    assert sorted(adapter.root for adapter in adapters) == sorted([home, extra])
    assert all(isinstance(adapter, AgyTranscriptSource) for adapter in adapters)
    assert config.discovery_roots["agy"][0] == str(home / "brain")
