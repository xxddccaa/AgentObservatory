from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from agent_observatory.db.models import AgentEvent, AgentTurn, CodexMessage, ToolCall
from agent_observatory.workers.indexer.adapters import PlanAction, SourcePlan
from agent_observatory.workers.indexer.claude import ClaudeJsonlSource
from agent_observatory.workers.indexer.claude_parser import iter_records


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))


def _rows() -> list[dict]:
    return [
        {"type": "user", "sessionId": "s-1", "cwd": "/tmp/demo", "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]}},
        {"type": "assistant", "message": {"role": "assistant", "model": "claude-3", "content": [{"type": "thinking", "thinking": "reason deeply"}, {"type": "text", "text": "Hi there"}, {"type": "tool_use", "id": "toolu-1", "name": "Bash", "input": {"command": "echo hi"}}], "usage": {"input_tokens": 3, "cache_read_input_tokens": 2, "output_tokens": 4}}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu-1", "content": [{"type": "text", "text": "hi"}]}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}], "usage": {"input_tokens": 1, "output_tokens": 2}}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "next"}]}},
        {"type": "mystery", "payload": {"long": "x" * 10000}},
    ]


def test_discovery_identity_and_bounded_parser(tmp_path: Path) -> None:
    path = tmp_path / "projects" / "cwd" / "session.jsonl"
    path.parent.mkdir(parents=True)
    _write(path, _rows())
    claude = ClaudeJsonlSource(tmp_path / "projects", agent="claude")
    stepcode = ClaudeJsonlSource(tmp_path / "projects", agent="stepcode")
    assert len(claude.discover()) == 1
    assert claude.discover()[0].identity != stepcode.discover()[0].identity
    records = list(iter_records(path, max_bytes=100))
    assert records and records[0].line_number == 1
    assert all(r.byte_offset >= 0 for r in records)


def test_parent_session_from_subagent_path(tmp_path: Path) -> None:
    child = tmp_path / "parent-thread" / "subagents" / "agent-123.jsonl"
    assert ClaudeJsonlSource.parent_session_from_path(child) == "parent-thread"
    assert ClaudeJsonlSource.parent_session_from_path(tmp_path / "session.jsonl") is None


def test_plan_append_and_rebuild(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    _write(path, _rows()[:1])
    adapter = ClaudeJsonlSource(tmp_path, agent="claude")
    source = adapter.discover()[0]
    first = adapter.plan(source, None)
    assert first.action == PlanAction.REBUILD
    path.write_text(path.read_text() + json.dumps(_rows()[1]) + "\n")
    assert adapter.plan(source, type("C", (), {"parser_version": "1", "status": "indexed", "cursor_json": {"byte_offset": first.fingerprint["size"]}, "fingerprint_json": first.fingerprint})()).action == PlanAction.APPEND


@pytest.mark.asyncio
async def test_ingest_projection_and_tool_pairing(tmp_path: Path, db_session) -> None:
    path = tmp_path / "session.jsonl"
    _write(path, _rows())
    session = await ClaudeJsonlSource(tmp_path, agent="stepcode").ingest(
        db_session,
        ClaudeJsonlSource(tmp_path, agent="stepcode").plan(
            ClaudeJsonlSource(tmp_path, agent="stepcode").discover()[0], None
        ),
    )
    assert session.agent == "stepcode"
    assert session.turn_count == 2
    assert session.input_tokens == 4
    turns = (await db_session.scalars(select(AgentTurn).where(AgentTurn.agent_session_id == session.id))).all()
    assert all(turn.agent == "stepcode" for turn in turns)
    tool = await db_session.scalar(select(ToolCall).where(ToolCall.agent_session_id == session.id))
    assert tool and tool.call_id == "toolu-1" and tool.output_text == "hi" and tool.status == "ok"
    messages = (await db_session.scalars(select(CodexMessage).where(CodexMessage.agent_session_id == session.id))).all()
    assert any("reason deeply" in message.text for message in messages)
    unknown = await db_session.scalar(select(AgentEvent).where(AgentEvent.normalized_type == "unknown"))
    assert unknown and unknown.raw_json["payload"]["long"].startswith("x")


@pytest.mark.anyio
@pytest.mark.parametrize("agent", ["claude", "stepcode"])
async def test_replayed_tool_use_preserves_one_completed_tool_and_all_raw_events(
    tmp_path: Path, db_session, agent: str
) -> None:
    path = tmp_path / "replayed.jsonl"
    rows = _rows()
    tool_block = rows[1]["message"]["content"][-1]
    rows[1]["message"]["content"].append(tool_block)
    replay = {"type": "assistant", "message": {"role": "assistant", "content": [tool_block]}}
    rows.insert(3, replay)
    _write(path, rows)
    adapter = ClaudeJsonlSource(tmp_path, agent=agent)
    source = adapter.discover()[0]
    session = await adapter.ingest(db_session, adapter.plan(source, None))
    await db_session.commit()
    with path.open("a") as handle:
        handle.write(json.dumps(replay) + "\n")
    await adapter.ingest(db_session, SourcePlan(source, PlanAction.APPEND, {}))
    await db_session.commit()
    tools = list((await db_session.scalars(select(ToolCall).where(ToolCall.agent_session_id == session.id))).all())
    events = list((await db_session.scalars(select(AgentEvent).where(AgentEvent.agent_session_id == session.id))).all())
    assert len(tools) == 1
    assert tools[0].agent == agent
    assert tools[0].status == "ok"
    assert tools[0].output_text == "hi"
    assert len(events) == len(rows) + 1
    assert session.input_tokens == 4
