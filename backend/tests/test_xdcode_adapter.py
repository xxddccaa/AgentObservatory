from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from agent_observatory.db.models import (
    AgentEvent,
    AgentSession,
    AgentTurn,
    IndexCheckpoint,
    ToolCall,
)
from agent_observatory.workers.indexer import IndexerWorker
from agent_observatory.workers.indexer.adapters import XDCodeJsonlSource
from agent_observatory.workers.indexer.scanner import RolloutScanner


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def _rows() -> list[dict]:
    return [
        {
            "ordinal": 0,
            "timestamp": "2026-08-21T10:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": "xd-thread",
                "session_id": "xd-thread",
                "cwd": "/tmp/xd-project",
                "originator": "xdcode",
                "cli_version": "1.2.3",
                "model_provider": "demo",
            },
        },
        {
            "ordinal": 1,
            "timestamp": "2026-08-21T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-1"},
        },
        {
            "ordinal": 2,
            "timestamp": "2026-08-21T10:00:02Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "inspect it"},
        },
        {
            "ordinal": 3,
            "timestamp": "2026-08-21T10:00:03Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"input_tokens": 9000, "output_tokens": 999},
                    "total_token_usage": {
                        "input_tokens": 9000,
                        "output_tokens": 999,
                        "total_tokens": 9999,
                    },
                },
            },
        },
        {
            "ordinal": 4,
            "timestamp": "2026-08-21T10:00:04Z",
            "type": "token_usage_record",
            "payload": {
                "thread_id": "xd-thread",
                "turn_id": "turn-1",
                "session_id": "xd-thread",
                "root_turn_id": "turn-1",
                "response_id": "resp-1",
                "usage": {
                    "input_tokens": 120,
                    "cached_input_tokens": 20,
                    "cache_write_input_tokens": 7,
                    "output_tokens": 25,
                    "reasoning_output_tokens": 5,
                    "total_tokens": 145,
                },
                "turn_token_usage": {
                    "input_tokens": 120,
                    "cached_input_tokens": 20,
                    "cache_write_input_tokens": 7,
                    "output_tokens": 25,
                    "reasoning_output_tokens": 5,
                    "total_tokens": 145,
                },
                "thread_token_usage": {
                    "input_tokens": 120,
                    "cached_input_tokens": 20,
                    "cache_write_input_tokens": 7,
                    "output_tokens": 25,
                    "reasoning_output_tokens": 5,
                    "total_tokens": 145,
                },
            },
        },
        {
            "ordinal": 5,
            "timestamp": "2026-08-21T10:00:05Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "done"}],
            },
        },
        {
            "ordinal": 6,
            "timestamp": "2026-08-21T10:00:06Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "turn-1"},
        },
        {
            "ordinal": 7,
            "timestamp": "2026-08-21T10:00:07Z",
            "type": "future_xd_event",
            "payload": {"keep": "raw"},
        },
    ]


def test_xdcode_identity_is_distinct_from_codex(tmp_path: Path) -> None:
    path = tmp_path / "sessions" / "rollout-xd.jsonl"
    _write(path, _rows()[:1])
    assert RolloutScanner.source_identity(path) != RolloutScanner.source_identity(
        path, "xdcode"
    )


def test_xdcode_archive_and_compression_keep_identity(tmp_path: Path) -> None:
    active = tmp_path / "xdcode" / "sessions" / "2026" / "rollout-unique.jsonl"
    archived = tmp_path / "xdcode" / "archived_sessions" / active.name
    assert RolloutScanner.source_identity(active, "xdcode") == RolloutScanner.source_identity(
        archived, "xdcode"
    )
    assert RolloutScanner.source_identity(active, "xdcode") == RolloutScanner.source_identity(
        archived.with_name(archived.name + ".zst"), "xdcode"
    )
    assert RolloutScanner.source_identity(active) != RolloutScanner.source_identity(archived)


@pytest.mark.anyio
async def test_worker_archive_move_keeps_one_session_and_checkpoint(tmp_path: Path, _test_engine):
    path = tmp_path / "xdcode" / "sessions" / "2026" / "rollout-unique.jsonl"
    archived = tmp_path / "xdcode" / "archived_sessions" / path.name
    rows = _rows()
    rows[6:6] = [
        {"type": "response_item", "payload": {
            "type": "function_call", "call_id": "archive-call", "name": "shell",
            "arguments": "{\"command\":\"printf synthetic\"}",
        }},
        {"type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "archive-call", "output": "synthetic output",
        }},
    ]
    _write(path, rows)
    original_scanner = RolloutScanner(path.parents[2])
    adapter = XDCodeJsonlSource(original_scanner)
    assert original_scanner.agent == "codex"
    worker = IndexerWorker(_test_engine, sessions_root=tmp_path / "empty")
    worker.orchestrator.sources = [adapter]
    assert await worker.scan_once() == 1
    async with _test_engine() as db:
        session = await db.scalar(select(AgentSession))
        session_id = session.id
        checkpoint = await db.scalar(select(IndexCheckpoint))
        checkpoint_id = checkpoint.id
        tool = await db.scalar(select(ToolCall))
        assert tool.agent == "xdcode"
    archived.parent.mkdir(parents=True)
    path.replace(archived)
    assert await worker.scan_once() == 1
    assert await worker.scan_once() == 0
    async with _test_engine() as db:
        sessions = list((await db.scalars(select(AgentSession))).all())
        checkpoints = list((await db.scalars(select(IndexCheckpoint))).all())
        assert len(sessions) == len(checkpoints) == 1
        assert sessions[0].id == session_id
        assert sessions[0].source_path == str(archived.resolve())
        assert sessions[0].total_tokens == 145
        assert sessions[0].agent == "xdcode"
        assert sessions[0].last_indexed_line == len(rows)
        assert checkpoints[0].id == checkpoint_id
        assert checkpoints[0].fingerprint_json["path"] == str(archived.resolve())


@pytest.mark.anyio
async def test_xdcode_indexes_usage_once_and_keeps_unknown_events(
    tmp_path: Path, db_session
) -> None:
    root = tmp_path / "sessions"
    path = root / "2026" / "08" / "21" / "rollout-xd.jsonl"
    _write(path, _rows())
    scanner = RolloutScanner(
        root,
        agent="xdcode",
        source_kind="xdcode_jsonl",
        identity_prefix="xdcode",
    )
    adapter = XDCodeJsonlSource(scanner)
    source = adapter.discover()[0]
    session = await adapter.ingest(db_session, adapter.plan(source, None))
    await db_session.flush()

    assert session.agent == "xdcode"
    assert session.source_kind == "xdcode_jsonl"
    assert session.thread_id == "xd-thread"
    assert session.input_tokens == 120
    assert session.cached_input_tokens == 20
    assert session.cache_creation_input_tokens == 7
    assert session.output_tokens == 25
    assert session.reasoning_output_tokens == 5
    assert session.total_tokens == 145

    turn = await db_session.scalar(
        select(AgentTurn).where(AgentTurn.agent_session_id == session.id)
    )
    assert turn is not None
    assert turn.total_tokens == 145

    events = list(
        (
            await db_session.scalars(
                select(AgentEvent).where(AgentEvent.agent_session_id == session.id)
            )
        ).all()
    )
    assert len(events) == len(_rows())
    usage_event = next(
        event for event in events if event.top_level_type == "token_usage_record"
    )
    assert usage_event.is_known is True
    unknown = next(event for event in events if event.top_level_type == "future_xd_event")
    assert unknown.normalized_type == "unknown"
    assert unknown.raw_json["payload"]["keep"] == "raw"

    await adapter.ingest(db_session, adapter.plan(source, None))
    await db_session.flush()
    sessions = list(
        (
            await db_session.scalars(
                select(AgentSession).where(AgentSession.thread_id == "xd-thread")
            )
        ).all()
    )
    assert len(sessions) == 1


@pytest.mark.anyio
async def test_xdcode_waits_for_partial_line_and_deduplicates_response_usage(tmp_path: Path, db_session):
    path = tmp_path / "rollout-partial.jsonl"
    rows = _rows()[:3]
    usage = _rows()[4]
    usage["payload"].pop("turn_token_usage")
    usage["payload"].pop("thread_token_usage")
    usage["payload"]["usage"].pop("total_tokens")
    _write(path, rows)
    encoded = json.dumps(usage)
    with path.open("a") as handle:
        handle.write(encoded[:20])
    adapter = XDCodeJsonlSource(RolloutScanner(tmp_path, agent="xdcode", source_kind="xdcode_jsonl", identity_prefix="xdcode"))
    source = adapter.discover()[0]
    session = await adapter.ingest(db_session, adapter.plan(source, None))
    assert session.last_indexed_line == len(rows)
    assert session.index_status == "indexed"
    with path.open("a") as handle:
        handle.write(encoded[20:] + "\n" + encoded + "\n")
    await adapter.ingest(db_session, adapter.plan(source, None))
    assert session.last_indexed_line == len(rows) + 2
    assert session.total_tokens == 145
    assert session.input_tokens == 120


@pytest.mark.anyio
async def test_worker_preserves_xdcode_identity_and_resumes_after_restart(tmp_path: Path, _test_engine):
    path = tmp_path / "xdcode" / "rollout-worker.jsonl"
    _write(path, _rows())
    adapter = XDCodeJsonlSource(RolloutScanner(path.parent, agent="xdcode", source_kind="xdcode_jsonl", identity_prefix="xdcode"))
    worker = IndexerWorker(_test_engine, sessions_root=tmp_path / "empty")
    worker.orchestrator.sources = [adapter]
    assert await worker.scan_once() == 1
    async with _test_engine() as db:
        session = await db.scalar(select(AgentSession).where(AgentSession.agent == "xdcode"))
        assert session.source_identity.startswith("xdcode:")
        assert session.source_kind == "xdcode_jsonl"
        assert session.total_tokens == 145
        turn = await db.scalar(select(AgentTurn).where(AgentTurn.agent_session_id == session.id))
        assert turn.agent == "xdcode"
    restarted = IndexerWorker(_test_engine, sessions_root=tmp_path / "empty")
    restarted.orchestrator.sources = [adapter]
    assert await restarted.scan_once() == 0
    with path.open("a") as handle:
        handle.write(json.dumps({"type": "future_after_restart", "payload": {"keep": True}}) + "\n")
    assert await restarted.scan_once() == 1
    async with _test_engine() as db:
        session = await db.scalar(select(AgentSession).where(AgentSession.agent == "xdcode"))
        assert session.last_indexed_line == len(_rows()) + 1
        assert session.total_tokens == 145


@pytest.mark.anyio
async def test_worker_deduplicates_overlapping_roots_by_real_file(tmp_path: Path, _test_engine):
    from agent_observatory.workers.indexer.adapters import CodexJsonlSource

    real = tmp_path / "native" / "rollout-shared.jsonl"
    _write(real, _rows())
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / real.name).symlink_to(real)
    worker = IndexerWorker(_test_engine, sessions_root=real.parent)
    worker.orchestrator.sources = [
        CodexJsonlSource(worker.scanner),
        XDCodeJsonlSource(RolloutScanner(shared, agent="xdcode", identity_prefix="xdcode")),
    ]
    pairs, duplicates = await worker.orchestrator._collect_sources()
    assert len(pairs) == 1
    assert pairs[0][1].agent == "codex"
    assert duplicates == 1
