from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from agent_observatory.db.models import AgentEvent
from agent_observatory.workers.indexer import RolloutScanner, parse_json_line


def test_unknown_event_keeps_raw_json() -> None:
    event = parse_json_line('{"type":"future_event","payload":{"answer":42}}')
    assert event.normalized_type == "unknown"
    assert event.is_known is False
    assert event.raw_json["payload"]["answer"] == 42


def test_nul_character_escaped_in_raw_json() -> None:
    # Codex tool output can contain literal NUL bytes (e.g. grepping binary
    # files); PostgreSQL jsonb cannot store them, so preserve a visible escaped
    # representation rather than silently dropping content.
    event = parse_json_line(
        '{"type":"response_item","payload":{"type":"function_call_output",'
        '"output":"before\\u0000after","nested":["x\\u0000y"]}}'
    )
    assert event.raw_json["payload"]["output"] == "before\\u0000after"
    assert event.raw_json["payload"]["nested"] == ["x\\u0000y"]
    assert event.normalized_type == "function_call_output"


@pytest.mark.anyio
async def test_incremental_index_and_duplicate_free(
    tmp_path: Path, db_session
) -> None:
    root = tmp_path / "sessions" / "2026" / "08" / "21"
    root.mkdir(parents=True)
    path = root / "rollout-demo.jsonl"
    records = [
        {"timestamp": "2026-08-21T10:00:00Z", "type": "session_meta", "payload": {"id": "thread-demo", "cwd": str(tmp_path)}},
        {"timestamp": "2026-08-21T10:00:01Z", "type": "future_event", "payload": {"value": "keep"}},
    ]
    path.write_text("\n".join(json.dumps(item) for item in records) + "\n")
    scanner = RolloutScanner(root)
    session = await scanner.index_path(db_session, path)
    await db_session.flush()
    events = list((await db_session.scalars(select(AgentEvent))).all())
    assert session.thread_id == "thread-demo"
    assert len(events) == 2
    assert events[1].raw_json["payload"]["value"] == "keep"
    await scanner.index_path(db_session, path)
    await db_session.flush()
    assert len(list((await db_session.scalars(select(AgentEvent))).all())) == 2


@pytest.mark.anyio
async def test_codex_subagent_meta_keeps_child_thread_and_parent(
    tmp_path: Path, db_session
) -> None:
    root = tmp_path / "sessions"
    root.mkdir(parents=True)
    path = root / "rollout-subagent.jsonl"
    path.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-21T10:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "child-thread",
                    "session_id": "parent-thread",
                    "cwd": str(tmp_path),
                },
            }
        )
        + "\n"
    )
    session = await RolloutScanner(root).index_path(db_session, path)
    assert session.thread_id == "child-thread"
    assert session.parent_session_id == "parent-thread"
    assert session.is_subagent is True


@pytest.mark.anyio
async def test_incremental_append_starts_at_exact_next_byte(
    tmp_path: Path, db_session
) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    path = root / "rollout-append.jsonl"
    first = {
        "timestamp": "2026-08-21T10:00:00Z",
        "type": "session_meta",
        "payload": {"id": "thread-append"},
    }
    second = {
        "timestamp": "2026-08-21T10:00:01Z",
        "type": "event_msg",
        "payload": {"type": "user_message", "message": "second"},
    }
    path.write_text(json.dumps(first) + "\n")
    scanner = RolloutScanner(root)
    session = await scanner.index_path(db_session, path)
    assert session.last_indexed_offset == path.stat().st_size

    with path.open("a") as handle:
        handle.write(json.dumps(second) + "\n")
    await scanner.index_path(db_session, path)
    events = list(
        (
            await db_session.scalars(
                select(AgentEvent)
                .where(AgentEvent.agent_session_id == session.id)
                .order_by(AgentEvent.event_index)
            )
        ).all()
    )
    assert len(events) == 2
    assert events[1].raw_json["payload"]["message"] == "second"
    assert session.last_indexed_offset == path.stat().st_size


@pytest.mark.anyio
async def test_partial_final_line_is_retried_not_marked_parse_error(
    tmp_path: Path, db_session
) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    path = root / "rollout-partial.jsonl"
    first = {
        "timestamp": "2026-08-21T10:00:00Z",
        "type": "session_meta",
        "payload": {"id": "thread-partial"},
    }
    path.write_text(json.dumps(first) + "\n" + '{"type":"event_msg"')
    scanner = RolloutScanner(root)
    session = await scanner.index_path(db_session, path)
    assert session.index_status == "indexed"
    assert session.last_error is None
    first_offset = session.last_indexed_offset

    with path.open("a") as handle:
        handle.write(',"payload":{"type":"warning","message":"done"}}\n')
    await scanner.index_path(db_session, path)
    assert session.last_indexed_offset > first_offset
    assert session.index_status == "indexed"
    assert session.last_error is None
