from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from agent_observatory.db.models import AgentEvent, AgentTurn, CodexMessage
from agent_observatory.workers.indexer.adapters import PlanAction
from agent_observatory.workers.indexer.stepcli import StepCliNativeSource


def _write_session(root: Path, session_id: str = "native-1") -> Path:
    session_dir = root / session_id
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text(
        json.dumps(
            {
                "schemaVersion": 4,
                "savedAt": "2026-09-03T08:49:29.957Z",
                "workspaceRoot": "/tmp/native-project",
                "provider": "anthropic",
                "model": "water18",
                "runtime": {"sessionId": session_id},
                "memory": {
                    "messages": [
                        {"role": "user", "content": "fallback prompt"},
                        {"role": "assistant", "content": "fallback reply"},
                    ]
                },
            }
        )
    )
    (session_dir / "events.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "eventId": "e1",
                        "kind": "session.message",
                        "recordedAt": "2026-09-03T08:49:09.630Z",
                        "payload": {
                            "message": {
                                "role": "user",
                                "content": "native prompt",
                            }
                        },
                    }
                ),
                json.dumps(
                    {
                        "eventId": "e2",
                        "kind": "session.message",
                        "recordedAt": "2026-09-03T08:49:09.631Z",
                        "payload": {
                            "message": {
                                "role": "assistant",
                                "content": "native reply",
                            }
                        },
                    }
                ),
                json.dumps(
                    {
                        "eventId": "e3",
                        "kind": "session.custom.unknown",
                        "recordedAt": "2026-09-03T08:49:09.632Z",
                        "payload": {"opaque": True},
                    }
                ),
            ]
        )
        + "\n"
    )
    return session_dir


def test_stepcli_discovers_and_plans_native_sessions(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    session_dir = _write_session(root)
    adapter = StepCliNativeSource(root)
    source = adapter.discover()[0]

    assert source.path == session_dir / "session.json"
    assert source.thread_id == "native-1"
    assert source.agent == "stepcli"
    initial = adapter.plan(source, None)
    assert initial.action == PlanAction.REBUILD

    class Checkpoint:
        parser_version = "1"
        status = "indexed"
        fingerprint_json = initial.fingerprint

    assert adapter.plan(source, Checkpoint()).action == PlanAction.UNCHANGED


@pytest.mark.anyio
async def test_stepcli_ingest_projects_messages_turns_and_raw_events(
    tmp_path: Path, db_session
) -> None:
    root = tmp_path / "sessions"
    _write_session(root)
    adapter = StepCliNativeSource(root)
    source = adapter.discover()[0]
    session = await adapter.ingest(
        db_session,
        adapter.plan(source, None),
    )

    assert session.agent == "stepcli"
    assert session.source_kind == "stepcli_json"
    assert session.thread_id == "native-1"
    assert session.cwd == "/tmp/native-project"
    assert session.model == "water18"
    assert session.display_title == "native prompt"
    assert session.turn_count == 1

    events = (
        await db_session.scalars(
            select(AgentEvent).where(AgentEvent.agent_session_id == session.id)
        )
    ).all()
    assert len(events) == 3
    assert events[2].raw_json["payload"]["opaque"] is True

    messages = (
        await db_session.scalars(
            select(CodexMessage)
            .where(CodexMessage.agent_session_id == session.id)
            .order_by(CodexMessage.sequence)
        )
    ).all()
    assert [(row.role, row.text) for row in messages] == [
        ("user", "native prompt"),
        ("assistant", "native reply"),
    ]
    turns = (
        await db_session.scalars(
            select(AgentTurn).where(AgentTurn.agent_session_id == session.id)
        )
    ).all()
    assert len(turns) == 1
    assert turns[0].outcome == "completed"


@pytest.mark.anyio
async def test_stepcli_snapshot_fallback_when_events_are_missing(
    tmp_path: Path, db_session
) -> None:
    root = tmp_path / "sessions"
    session_dir = _write_session(root, "native-fallback")
    (session_dir / "events.jsonl").unlink()
    adapter = StepCliNativeSource(root)
    source = adapter.discover()[0]
    session = await adapter.ingest(db_session, adapter.plan(source, None))

    messages = (
        await db_session.scalars(
            select(CodexMessage).where(CodexMessage.agent_session_id == session.id)
        )
    ).all()
    assert [message.text for message in messages] == [
        "fallback prompt",
        "fallback reply",
    ]
