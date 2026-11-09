import json
from pathlib import Path

import pytest
import zstandard as zstd
from sqlalchemy import select

from agent_observatory.db.models import AgentEvent, AgentTurn, CodexMessage, ToolCall
from agent_observatory.workers.indexer.adapters import PlanAction, SourcePlan
from agent_observatory.workers.indexer.dsh import DshJsonlSource


@pytest.mark.asyncio
async def test_dsh_ingest_projects_records(tmp_path: Path, db_session):
    path = tmp_path / "x" / "session.jsonl.zstd"
    path.parent.mkdir()
    rows = [
        {
            "type": "session",
            "id": "s1",
            "createdAt": "2026-01-01T00:00:00Z",
            "cwd": "/repo",
            "parentSession": "p",
        },
        {
            "type": "turn/start",
            "data": {"turn": 1},
            "time": "2026-01-01T00:00:01Z",
        },
        {
            "type": "user/message",
            "data": {
                "turn": 1,
                "role": "user",
                "source": {"kind": "user"},
                "content": [{"type": "text", "text": "hello"}],
            },
        },
        {
            "type": "request/context",
            "data": {"provider": "stepcode", "model": "dsh-model"},
        },
        {
            "type": "assistant/message",
            "data": {
                "turn": 1,
                "step": 1,
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "reasoning", "text": "think"},
                        {"type": "text", "text": "world"},
                    ],
                },
                "usage": {"inputTokens": 2, "outputTokens": 3},
            },
        },
        {
            "type": "tool/call",
            "data": {
                "turn": 1,
                "step": 1,
                "callId": "c1",
                "name": "shell",
                "arguments": '{"cmd":"ls"}',
            },
        },
        {
            "type": "tool/result",
            "data": {
                "turn": 1,
                "step": 1,
                "message": {
                    "role": "tool",
                    "content": [
                        {
                            "type": "tool-result",
                            "toolCallId": "c1",
                            "content": [{"type": "text", "text": "ok"}],
                        }
                    ],
                },
            },
        },
        {
            "type": "turn/end",
            "data": {"turn": 1, "reason": {"kind": "completed"}},
        },
        {"type": "mystery", "data": {"x": "nul:\u0000"}},
    ]
    payload = ("\n".join(json.dumps(r) for r in rows) + "\n").encode()
    path.write_bytes(zstd.ZstdCompressor().compress(payload))
    adapter = DshJsonlSource(tmp_path)
    source = adapter.discover()[0]
    session = await adapter.ingest(db_session, SourcePlan(source, PlanAction.REBUILD, {}))
    assert session.agent == "dsh"
    assert session.cwd == "/repo"
    assert session.parent_session_id == "p"
    assert session.model == "dsh-model"
    assert session.total_tokens == 5
    messages = (await db_session.scalars(select(CodexMessage))).all()
    assert {message.channel for message in messages} == {"final", "reasoning"}
    assert (await db_session.scalars(select(AgentTurn))).all()
    tool = (await db_session.scalars(select(ToolCall))).one()
    assert tool.output_text == "ok"
    assert tool.status == "completed"
    assert (await db_session.scalars(select(AgentEvent))).all()[-1].is_known is False
    assert (
        await db_session.scalars(select(AgentEvent))
    ).all()[-1].raw_json["data"]["x"] == "nul:\\u0000"
