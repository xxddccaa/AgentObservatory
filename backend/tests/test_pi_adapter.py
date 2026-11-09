from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from agent_observatory.db.models import AgentEvent, AgentTurn, CodexMessage, ToolCall
from agent_observatory.workers.indexer.adapters import PlanAction
from agent_observatory.workers.indexer.pi import PiJsonlSource


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def _rows() -> list[dict]:
    return [
        {
            "type": "session",
            "version": 3,
            "id": "pi-session-1",
            "timestamp": "2026-08-21T10:00:00Z",
            "cwd": "/tmp/pi-project",
        },
        {
            "type": "model_change",
            "id": "m1",
            "parentId": None,
            "timestamp": "2026-08-21T10:00:01Z",
            "provider": "demo",
            "modelId": "pi-model",
        },
        {
            "type": "thinking_level_change",
            "id": "t1",
            "parentId": "m1",
            "timestamp": "2026-08-21T10:00:02Z",
            "thinkingLevel": "high",
        },
        {
            "type": "message",
            "id": "sys1",
            "parentId": "t1",
            "timestamp": "2026-08-21T10:00:03Z",
            "message": {
                "role": "system",
                "content": [{"type": "text", "text": "system policy"}],
            },
        },
        {
            "type": "message",
            "id": "u1",
            "parentId": "sys1",
            "timestamp": "2026-08-21T10:00:04Z",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "hello"}],
            },
        },
        {
            "type": "message",
            "id": "a1",
            "parentId": "u1",
            "timestamp": "2026-08-21T10:00:05Z",
            "message": {
                "role": "assistant",
                "model": "pi-model",
                "stopReason": "toolUse",
                "content": [
                    {"type": "thinking", "thinking": "reasoning summary"},
                    {"type": "text", "text": "working"},
                    {
                        "type": "toolCall",
                        "id": "call-1",
                        "name": "bash",
                        "arguments": {"command": "printf ok"},
                    },
                ],
                "usage": {
                    "input": 3,
                    "output": 10,
                    "cacheRead": 2,
                    "cacheWrite": 1,
                    "reasoning": 5,
                    "totalTokens": 16,
                },
            },
        },
        {
            "type": "message",
            "id": "abandoned",
            "parentId": "a1",
            "timestamp": "2026-08-21T10:00:06Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "abandoned branch"}],
                "stopReason": "stop",
            },
        },
        {
            "type": "message",
            "id": "tr1",
            "parentId": "a1",
            "timestamp": "2026-08-21T10:00:07Z",
            "message": {
                "role": "toolResult",
                "toolCallId": "call-1",
                "toolName": "bash",
                "content": [{"type": "text", "text": "ok"}],
                "details": {"exitCode": 0},
                "isError": False,
            },
        },
        {
            "type": "message",
            "id": "a2",
            "parentId": "tr1",
            "timestamp": "2026-08-21T10:00:08Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "done"}],
                "stopReason": "stop",
                "usage": {
                    "input": 1,
                    "output": 2,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                    "reasoning": 0,
                    "totalTokens": 3,
                },
            },
        },
        {
            "type": "future_custom",
            "id": "unknown-1",
            "parentId": "a2",
            "timestamp": "2026-08-21T10:00:09Z",
            "payload": {"opaque": True},
        },
        {
            "type": "compaction",
            "id": "compact-1",
            "parentId": "unknown-1",
            "timestamp": "2026-08-21T10:00:10Z",
            "summary": "compacted context",
        },
    ]


class _Checkpoint:
    parser_version = "2"
    status = "indexed"

    def __init__(self, fingerprint: dict, cursor: dict) -> None:
        self.fingerprint_json = fingerprint
        self.cursor_json = cursor


@pytest.mark.anyio
async def test_pi_projection_uses_active_branch_and_dedupes_tokens(
    tmp_path: Path, db_session
) -> None:
    path = tmp_path / "sessions" / "session-pi.jsonl"
    _write(path, _rows())
    adapter = PiJsonlSource(tmp_path / "sessions")
    source = adapter.discover()[0]
    session = await adapter.ingest(db_session, adapter.plan(source, None))

    assert session.agent == "pi"
    assert session.source_kind == "pi_jsonl"
    assert session.thread_id == "pi-session-1"
    assert session.model == "pi-model"
    assert session.display_title == "hello"
    assert session.turn_count == 1
    assert session.input_tokens == 4
    assert session.cached_input_tokens == 2
    assert session.cache_creation_input_tokens == 1
    assert session.output_tokens == 12
    assert session.reasoning_output_tokens == 5
    assert session.total_tokens == 19
    assert session.is_ongoing is False

    messages = list(
        (
            await db_session.scalars(
                select(CodexMessage)
                .where(CodexMessage.agent_session_id == session.id)
                .order_by(CodexMessage.sequence)
            )
        ).all()
    )
    texts = [message.text for message in messages]
    assert "system policy" in texts
    assert "working" in texts
    assert "done" in texts
    assert "compacted context" in texts
    assert "abandoned branch" not in texts
    assert any(message.channel == "reasoning" for message in messages)

    tool = await db_session.scalar(
        select(ToolCall).where(ToolCall.agent_session_id == session.id)
    )
    assert tool is not None
    assert tool.call_id == "call-1"
    assert tool.output_text == "ok"
    assert tool.status == "ok"
    assert tool.exit_code == 0

    unknown = await db_session.scalar(
        select(AgentEvent).where(
            AgentEvent.agent_session_id == session.id,
            AgentEvent.normalized_type == "unknown",
        )
    )
    assert unknown is not None
    assert unknown.raw_json["payload"]["opaque"] is True

    first_fingerprint = dict(adapter.plan(source, None).fingerprint)
    cursor = {
        "byte_offset": session.last_indexed_offset,
        "line_number": session.last_indexed_line,
    }
    with path.open("a") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "message",
                    "id": "u2",
                    "parentId": "compact-1",
                    "timestamp": "2026-08-21T10:01:00Z",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "second"}],
                    },
                }
            )
            + "\n"
        )
    assert (
        adapter.plan(source, _Checkpoint(first_fingerprint, cursor)).action
        == PlanAction.APPEND
    )
    await adapter.ingest(
        db_session, adapter.plan(source, _Checkpoint(first_fingerprint, cursor))
    )
    await db_session.flush()
    events = list(
        (
            await db_session.scalars(
                select(AgentEvent).where(
                    AgentEvent.agent_session_id == session.id
                )
            )
        ).all()
    )
    assert len(events) == len(_rows()) + 1
    turns = list(
        (
            await db_session.scalars(
                select(AgentTurn).where(AgentTurn.agent_session_id == session.id)
            )
        ).all()
    )
    assert len(turns) == 2


def test_pi_partial_line_is_retried(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    _write(path, _rows()[:1])
    with path.open("a") as handle:
        handle.write('{"type":"message"')
    from agent_observatory.workers.indexer.pi_parser import iter_records

    records = list(iter_records(path))
    assert len(records) == 1


def test_pi_oversized_lines_have_bounded_memory(tmp_path: Path, monkeypatch) -> None:
    import tracemalloc

    from agent_observatory.workers.indexer import pi_parser

    monkeypatch.setattr(pi_parser, "_MAX_LINE_BYTES", 1024)
    path = tmp_path / "large.jsonl"
    with path.open("wb") as handle:
        for _index in range(1024):
            handle.write(b"x" * 4096)
        handle.write(b'\n{"type":"custom","id":"tail"}\n')
    tracemalloc.start()
    try:
        records = list(pi_parser.iter_records(path))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1024 * 1024
    assert records[0].raw_json["error"] == "line_too_large"
    assert records[1].record_id == "tail"
    assert records[1].byte_offset == 4 * 1024 * 1024 + 1


@pytest.mark.anyio
async def test_pi_growing_rewrite_rebuilds_and_clears_metadata(tmp_path: Path, db_session):
    path = tmp_path / "session.jsonl"
    _write(path, _rows())
    adapter = PiJsonlSource(tmp_path)
    source = adapter.discover()[0]
    first_plan = adapter.plan(source, None)
    session = await adapter.ingest(db_session, first_plan)
    checkpoint = _Checkpoint(first_plan.fingerprint, {"byte_offset": session.last_indexed_offset})
    rows = [
        {"type": "session", "id": "rewritten", "timestamp": "2026-08-22T10:00:00Z"},
        {"type": "message", "id": "new-user", "parentId": None,
         "message": {"role": "user", "content": "new " + "x" * path.stat().st_size}},
    ]
    _write(path, rows)
    plan = adapter.plan(source, checkpoint)
    assert plan.action == PlanAction.REBUILD
    await adapter.ingest(db_session, plan)
    assert session.thread_id == "rewritten"
    assert session.model is None
    assert session.cwd is None
    assert session.cli_version is None
    assert session.thread_name is None
    assert session.total_tokens == 0
    assert session.last_indexed_line == 2
