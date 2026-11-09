"""Projection + export/search coverage using the real Codex event taxonomy."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from agent_observatory.db.models import (
    ActivitySpan,
    AgentEvent,
    AgentTurn,
    CodexMessage,
    EvidenceRecord,
    LocalDataSource,
    SessionSkillEvidence,
    SkillDefinition,
    ToolCall,
)
from agent_observatory.workers.indexer import RolloutScanner


def _completed_rollout() -> list[dict]:
    """A one-turn rollout that starts and completes, mixing event_msg and
    response_item sources exactly like real Codex output."""
    return [
        {
            "timestamp": "2026-08-21T10:00:00.000Z",
            "type": "session_meta",
            "payload": {
                "id": "thread-proj",
                "session_id": "thread-proj",
                "cwd": "/tmp/proj",
                "originator": "codex-tui",
                "cli_version": "0.148.0",
                "model_provider": "openai",
                "git": {"branch": "main"},
            },
        },
        {
            "timestamp": "2026-08-21T10:00:01.000Z",
            "type": "turn_context",
            "payload": {"turn_id": "turn-1", "model": "gpt-5.5"},
        },
        {
            "timestamp": "2026-08-21T10:00:02.000Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-1", "started_at": 1780000000},
        },
        {
            "timestamp": "2026-08-21T10:00:03.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Please inspect the directory."},
        },
        {
            "timestamp": "2026-08-21T10:00:04.000Z",
            "type": "event_msg",
            "payload": {"type": "agent_reasoning", "text": "**Plan**\nList the files first."},
        },
        {
            "timestamp": "2026-08-21T10:00:05.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": "ls -la /tmp/directory", "workdir": "/tmp/proj"}),
                "call_id": "call_1",
            },
        },
        {
            "timestamp": "2026-08-21T10:00:06.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "total 0\n/tmp/directory\nProcess exited with code 0\n",
            },
        },
        {
            "timestamp": "2026-08-21T10:00:07.000Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1200,
                        "cached_input_tokens": 0,
                        "output_tokens": 340,
                        "reasoning_output_tokens": 50,
                        "total_tokens": 1540,
                    },
                    "last_token_usage": {
                        "input_tokens": 1200,
                        "cached_input_tokens": 0,
                        "output_tokens": 340,
                        "reasoning_output_tokens": 50,
                        "total_tokens": 1540,
                    },
                },
            },
        },
        {
            "timestamp": "2026-08-21T10:00:08.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "The directory is empty."}],
            },
        },
        {
            "timestamp": "2026-08-21T10:00:09.000Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn-1",
                "last_agent_message": "The directory is empty.",
            },
        },
    ]


def _write_rollout(tmp_path: Path, name: str, records: list[dict]) -> Path:
    root = tmp_path / "sessions" / "2026" / "08" / "21"
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text("\n".join(json.dumps(item) for item in records) + "\n")
    return path


async def _index(db_session, path: Path):
    scanner = RolloutScanner(path.parent)
    session = await scanner.index_path(db_session, path)
    await db_session.flush()
    return session


async def _messages(db_session, session_id: str) -> list[CodexMessage]:
    return list(
        (
            await db_session.scalars(
                select(CodexMessage)
                .where(CodexMessage.agent_session_id == session_id)
                .order_by(CodexMessage.sequence)
            )
        ).all()
    )


@pytest.mark.anyio
async def test_projection_builds_messages_tools_and_turn(tmp_path: Path, db_session) -> None:
    path = _write_rollout(tmp_path, "rollout-proj.jsonl", _completed_rollout())
    session = await _index(db_session, path)

    messages = await _messages(db_session, session.id)
    by_channel = {(m.role, m.channel): m for m in messages}

    # reasoning is projected on the reasoning channel
    reasoning = by_channel[("assistant", "reasoning")]
    assert reasoning.channel == "reasoning"
    assert "List the files first" in reasoning.text
    assert reasoning.markdown_text is not None

    # user + assistant final replies both projected
    assert by_channel[("user", "final")].text == "Please inspect the directory."
    assistant = by_channel[("assistant", "final")]
    assert assistant.text == "The directory is empty."
    assert assistant.copy_text == "The directory is empty."

    # tool call paired with its output
    tool = await db_session.scalar(
        select(ToolCall).where(ToolCall.agent_session_id == session.id)
    )
    assert tool.call_id == "call_1"
    assert tool.tool_name == "exec_command"
    assert tool.tool_kind == "exec"
    assert tool.command_text == "ls -la /tmp/directory"
    assert tool.output_text is not None and "total 0" in tool.output_text
    assert tool.exit_code == 0
    assert tool.status == "ok"
    assert tool.turn_id is not None

    # turn created and closed with per-turn tokens
    turns = list(
        (
            await db_session.scalars(
                select(AgentTurn).where(AgentTurn.agent_session_id == session.id)
            )
        ).all()
    )
    assert len(turns) == 1
    assert turns[0].status == "completed"
    assert turns[0].completed_at is not None
    assert turns[0].total_tokens == 1540

    # session aggregates + status
    assert session.turn_count == 1
    assert session.total_tokens == 1540
    assert session.model == "gpt-5.5"  # turn_context overrides the provider
    assert session.is_ongoing is False  # last turn completed
    assert session.end_time is not None


@pytest.mark.anyio
async def test_projection_builds_exact_activity_spans_and_validation_metrics(
    tmp_path: Path, db_session
) -> None:
    records = _completed_rollout()
    records.insert(
        6,
        {
            "timestamp": "2026-08-21T10:00:06.200Z",
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "thread_id": "thread-proj",
                "turn_id": "turn-1",
                "started_at_ms": 1_787_306_405_000,
                "completed_at_ms": 1_787_306_406_200,
                "item": {
                    "type": "CommandExecution",
                    "id": "item-test",
                    "command": ["uv", "run", "pytest", "-q"],
                    "status": "completed",
                    "exit_code": 0,
                },
            },
        },
    )
    path = _write_rollout(tmp_path, "rollout-metrics.jsonl", records)
    session = await _index(db_session, path)
    turn = await db_session.scalar(
        select(AgentTurn).where(AgentTurn.agent_session_id == session.id)
    )
    assert turn is not None
    assert turn.duration_ms == 7_000
    assert turn.first_agent_latency_ms == 2_000
    assert turn.active_tool_ms == 1_200
    assert turn.validation_status == "passed"
    assert turn.outcome == "completed"
    assert turn.metric_confidence == "derived"

    spans = list(
        (
            await db_session.scalars(
                select(ActivitySpan)
                .where(ActivitySpan.agent_session_id == session.id)
                .order_by(ActivitySpan.started_at)
            )
        ).all()
    )
    assert [(span.span_type, span.duration_ms) for span in spans] == [
        ("turn", 7_000),
        ("tool", 1_200),
        ("exec", 1_000),
    ]
    assert all(span.evidence_id for span in spans)


@pytest.mark.anyio
async def test_world_state_projects_available_skills_and_rollout_evidence(
    tmp_path: Path, db_session
) -> None:
    now = datetime.fromisoformat("2026-08-21T10:00:00+00:00")
    db_session.add(
        SkillDefinition(
            id="skill-imagegen",
            name="imagegen",
            description="Generate images",
            source_path="/tmp/skills/imagegen/SKILL.md",
            scope="system",
            content_hash="skill-hash",
            enabled=True,
            first_seen_at=now,
            last_seen_at=now,
        )
    )
    records = _completed_rollout()
    records.insert(
        1,
        {
            "timestamp": "2026-08-21T10:00:00.500Z",
            "type": "world_state",
            "payload": {
                "state": {
                    "host_skills": {
                        "body": (
                            "### Available skills\n"
                            "- imagegen: Generate local images. "
                            "(file: /tmp/skills/imagegen/SKILL.md)\n"
                            "- audit-helper: Inspect evidence. "
                            "(file: /tmp/skills/audit-helper/SKILL.md)\n"
                        )
                    }
                }
            },
        },
    )
    path = _write_rollout(tmp_path, "rollout-skills.jsonl", records)
    session = await _index(db_session, path)

    evidence = list(
        (
            await db_session.scalars(
                select(SessionSkillEvidence)
                .where(SessionSkillEvidence.agent_session_id == session.id)
                .order_by(SessionSkillEvidence.skill_name)
            )
        ).all()
    )
    assert [(row.skill_name, row.evidence_type, row.confidence) for row in evidence] == [
        ("audit-helper", "available", "exact"),
        ("imagegen", "available", "exact"),
    ]
    assert evidence[1].skill_id == "skill-imagegen"
    raw = await db_session.get(EvidenceRecord, evidence[0].evidence_id)
    assert raw is not None
    assert raw.raw_json is None
    assert raw.agent_event_id is not None


@pytest.mark.anyio
async def test_rollout_reprojection_preserves_local_source_spans(
    tmp_path: Path, db_session
) -> None:
    now = datetime.fromisoformat("2026-08-21T10:00:00+00:00")
    path = _write_rollout(tmp_path, "rollout-owned-spans.jsonl", _completed_rollout())
    session = await _index(db_session, path)
    source = LocalDataSource(
        id="source-local-test",
        source_kind="thread_history",
        source_identity="test:thread-history",
        source_path="/tmp/thread_history.sqlite",
        source_format="sqlite",
        parser_version="1",
        status="indexed",
    )
    db_session.add(source)
    await db_session.flush()
    local_evidence = EvidenceRecord(
        id="local-evidence",
        source_id=source.id,
        source_record_id="item:local",
        agent_session_id=session.id,
        thread_id=session.thread_id,
        category="thread_item",
        raw_text="local evidence",
        raw_hash="local-hash",
    )
    db_session.add(local_evidence)
    await db_session.flush()
    db_session.add(
        ActivitySpan(
            id="local-span",
            source_kind="thread_history",
            source_record_id="item:local",
            agent_session_id=session.id,
            evidence_id=local_evidence.id,
            thread_id=session.thread_id,
            turn_id="turn-1",
            span_type="tool",
            name="local tool",
            status="completed",
            started_at=now,
            completed_at=now + timedelta(seconds=1),
            duration_ms=1_000,
            confidence="exact",
        )
    )
    await db_session.flush()

    await _index(db_session, path)
    preserved = await db_session.get(ActivitySpan, "local-span")
    assert preserved is not None
    assert preserved.evidence_id == "local-evidence"


@pytest.mark.anyio
async def test_projection_marks_open_session_ongoing(tmp_path: Path, db_session) -> None:
    records = _completed_rollout()[:-2]  # drop the assistant reply + task_complete
    path = _write_rollout(tmp_path, "rollout-open.jsonl", records)
    session = await _index(db_session, path)

    turns = list(
        (
            await db_session.scalars(
                select(AgentTurn).where(AgentTurn.agent_session_id == session.id)
            )
        ).all()
    )
    assert turns[0].status == "running"
    # last turn never completed and the file was just written -> ongoing
    assert session.is_ongoing is True
    assert session.end_time is None


@pytest.mark.anyio
async def test_projection_is_idempotent(tmp_path: Path, db_session) -> None:
    path = _write_rollout(tmp_path, "rollout-idem.jsonl", _completed_rollout())
    session = await _index(db_session, path)
    first = await _messages(db_session, session.id)

    # re-index the unchanged file: projection rebuilds, no duplicate rows
    await _index(db_session, path)
    second = await _messages(db_session, session.id)
    assert len(second) == len(first)

    tools = list(
        (
            await db_session.scalars(
                select(ToolCall).where(ToolCall.agent_session_id == session.id)
            )
        ).all()
    )
    turns = list(
        (
            await db_session.scalars(
                select(AgentTurn).where(AgentTurn.agent_session_id == session.id)
            )
        ).all()
    )
    assert len(tools) == 1
    assert len(turns) == 1


@pytest.mark.anyio
async def test_duplicate_event_msg_and_response_item_deduped(tmp_path: Path, db_session) -> None:
    """Real rollouts emit the same assistant reply as both event_msg/agent_message
    and response_item/message; the projection must not double it."""
    records = _completed_rollout()
    records.insert(
        9,
        {
            "timestamp": "2026-08-21T10:00:08.500Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "The directory is empty."},
        },
    )
    path = _write_rollout(tmp_path, "rollout-dup.jsonl", records)
    session = await _index(db_session, path)
    finals = [
        m
        for m in await _messages(db_session, session.id)
        if m.role == "assistant" and m.channel == "final"
    ]
    assert len(finals) == 1


@pytest.mark.anyio
async def test_duplicate_assistant_message_is_deduped_across_intervening_event(
    tmp_path: Path, db_session
) -> None:
    records = _completed_rollout()
    records.insert(
        9,
        {
            "timestamp": "2026-08-21T10:00:08.200Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "The directory is empty."},
        },
    )
    records.insert(
        10,
        {
            "timestamp": "2026-08-21T10:00:08.300Z",
            "type": "event_msg",
            "payload": {"type": "warning", "message": "intervening event"},
        },
    )
    path = _write_rollout(tmp_path, "rollout-dup-separated.jsonl", records)
    session = await _index(db_session, path)
    finals = [
        message
        for message in await _messages(db_session, session.id)
        if message.role == "assistant" and message.channel == "final"
    ]
    assert len(finals) == 1


@pytest.mark.anyio
async def test_repeated_token_updates_replace_turn_usage(
    tmp_path: Path, db_session
) -> None:
    records = _completed_rollout()
    records.insert(
        8,
        {
            "timestamp": "2026-08-21T10:00:07.500Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1400,
                        "cached_input_tokens": 100,
                        "output_tokens": 400,
                        "reasoning_output_tokens": 60,
                        "total_tokens": 1800,
                    },
                    "last_token_usage": {
                        "input_tokens": 200,
                        "cached_input_tokens": 100,
                        "output_tokens": 60,
                        "reasoning_output_tokens": 10,
                        "total_tokens": 260,
                    },
                },
            },
        },
    )
    path = _write_rollout(tmp_path, "rollout-token-update.jsonl", records)
    session = await _index(db_session, path)
    turn = await db_session.scalar(
        select(AgentTurn).where(AgentTurn.agent_session_id == session.id)
    )
    assert turn is not None
    assert turn.total_tokens == 260
    assert session.total_tokens == 1800


@pytest.mark.anyio
async def test_output_before_call_is_paired_and_display_is_redacted(
    tmp_path: Path, db_session
) -> None:
    records = _completed_rollout()
    call_at = next(
        index
        for index, item in enumerate(records)
        if item.get("payload", {}).get("type") == "function_call"
    )
    output_at = next(
        index
        for index, item in enumerate(records)
        if item.get("payload", {}).get("type") == "function_call_output"
    )
    output = records.pop(output_at)
    call = records.pop(call_at)
    output["payload"]["output"] = (
        "token=supersecretvalue123\nProcess exited with code 0\n"
    )
    records.insert(4, output)
    records.insert(5, call)
    path = _write_rollout(tmp_path, "rollout-output-first.jsonl", records)
    session = await _index(db_session, path)
    tool = await db_session.scalar(
        select(ToolCall).where(ToolCall.agent_session_id == session.id)
    )
    assert tool is not None
    assert tool.status == "ok"
    assert tool.output_text is not None
    assert "supersecretvalue123" not in tool.output_text
    assert "[REDACTED_SECRET]" in tool.output_text

    raw_output = await db_session.scalar(
        select(AgentEvent).where(AgentEvent.payload_type == "function_call_output")
    )
    assert raw_output is not None
    assert "supersecretvalue123" in raw_output.raw_json["payload"]["output"]


@pytest.mark.anyio
async def test_nul_in_tool_arguments_is_postgres_safe(
    tmp_path: Path, db_session
) -> None:
    records = _completed_rollout()
    call = next(
        item
        for item in records
        if item.get("payload", {}).get("type") == "function_call"
    )
    call["payload"]["arguments"] = json.dumps(
        {"cmd": "printf 'a\\u0000b'", "metadata": {"raw": "x\x00y"}}
    )
    path = _write_rollout(tmp_path, "rollout-nul.jsonl", records)
    session = await _index(db_session, path)
    tool = await db_session.scalar(
        select(ToolCall).where(ToolCall.agent_session_id == session.id)
    )
    assert tool is not None
    assert "\x00" not in (tool.command_text or "")
    assert tool.arguments_json is not None
    assert tool.arguments_json["metadata"]["raw"] == "x\\u0000y"


@pytest.mark.anyio
async def test_v0149_item_completed_projects_user_and_agent_messages(
    tmp_path: Path, db_session
) -> None:
    records = [
        {
            "timestamp": "2026-08-21T10:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": "thread-v149",
                "timestamp": "2026-08-21T09:59:00Z",
                "cwd": str(tmp_path),
            },
        },
        {
            "timestamp": "2026-08-21T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-v149"},
        },
        {
            "timestamp": "2026-08-21T10:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "injected scaffolding"}],
            },
        },
        {
            "timestamp": "2026-08-21T10:00:03Z",
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "UserMessage",
                    "content": [{"type": "text", "text": "actual prompt"}],
                },
            },
        },
        {
            "timestamp": "2026-08-21T10:00:04Z",
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "AgentMessage",
                    "content": [{"type": "Text", "text": "actual reply"}],
                },
            },
        },
        {
            "timestamp": "2026-08-21T10:00:04.500Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "actual reply"}],
            },
        },
        {
            "timestamp": "2026-08-21T10:00:05Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "turn-v149"},
        },
    ]
    path = _write_rollout(tmp_path, "rollout-v149.jsonl", records)
    session = await _index(db_session, path)
    messages = await _messages(db_session, session.id)
    assert [(message.role, message.text) for message in messages] == [
        ("user", "actual prompt"),
        ("assistant", "actual reply"),
    ]


async def _login(client: AsyncClient, username: str, password: str) -> None:
    response = await client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text


@pytest.mark.anyio
async def test_export_endpoint_text_markdown_json(
    tmp_path: Path, client: AsyncClient, operator_user, db_session
) -> None:
    path = _write_rollout(tmp_path, "rollout-export.jsonl", _completed_rollout())
    session = await _index(db_session, path)
    await _login(client, operator_user["username"], "operator-password-123")

    text_resp = await client.get(f"/api/codex/sessions/{session.id}/export?format=text")
    assert text_resp.status_code == 200, text_resp.text
    text_body = text_resp.json()
    assert text_body["format"] == "text"
    assert text_body["filename"] == "codex-thread-proj.txt"
    assert "USER:" in text_body["content"]
    assert "Please inspect the directory." in text_body["content"]
    assert "The directory is empty." in text_body["content"]
    assert "$ ls -la /tmp/directory" in text_body["content"]

    md_resp = await client.get(f"/api/codex/sessions/{session.id}/export?format=markdown")
    assert md_resp.status_code == 200
    md_body = md_resp.json()
    assert md_body["filename"] == "codex-thread-proj.md"
    assert "## User" in md_body["content"]
    assert "#### Tool: `exec_command`" in md_body["content"]

    json_resp = await client.get(f"/api/codex/sessions/{session.id}/export?format=json")
    assert json_resp.status_code == 200
    json_body = json_resp.json()
    assert json_body["filename"] == "codex-thread-proj.json"
    events = json.loads(json_body["content"])
    assert isinstance(events, list)
    assert events[0]["type"] == "session_meta"
    assert len(events) == 10

    bad = await client.get(f"/api/codex/sessions/{session.id}/export?format=bogus")
    assert bad.status_code == 400


@pytest.mark.anyio
async def test_search_endpoint_ranks_messages_before_tools(
    tmp_path: Path, client: AsyncClient, operator_user, db_session
) -> None:
    path = _write_rollout(tmp_path, "rollout-search.jsonl", _completed_rollout())
    session = await _index(db_session, path)
    await _login(client, operator_user["username"], "operator-password-123")

    resp = await client.get(f"/api/codex/sessions/{session.id}/search?q=directory")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["query"] == "directory"
    kinds = [m["kind"] for m in body["matches"]]
    assert "message" in kinds
    assert "tool" in kinds
    # messages rank before tools
    assert kinds.index("message") < kinds.index("tool")
    first = body["matches"][0]
    assert first["kind"] == "message"
    assert "directory" in first["snippet"].lower()

    empty = await client.get(f"/api/codex/sessions/{session.id}/search?q=")
    assert empty.status_code == 200
    assert empty.json()["matches"] == []


@pytest.mark.anyio
async def test_messages_and_list_endpoints_expose_contract_fields(
    tmp_path: Path, client: AsyncClient, operator_user, db_session
) -> None:
    path = _write_rollout(tmp_path, "rollout-contract.jsonl", _completed_rollout())
    session = await _index(db_session, path)
    await _login(client, operator_user["username"], "operator-password-123")

    messages = (await client.get(f"/api/codex/sessions/{session.id}/messages")).json()["messages"]
    sample = messages[0]
    for field in ("id", "role", "channel", "text", "markdown_text", "copy_text", "sequence", "timestamp", "turn_id"):
        assert field in sample

    listing = (await client.get("/api/codex/sessions")).json()["sessions"]
    row = next(item for item in listing if item["id"] == session.id)
    for field in (
        "total_tokens",
        "is_ongoing",
        "updated_at",
        "activity_at",
        "turn_count",
        "index_status",
        "ai_title",
        "workspace_id",
        "workspace_kind",
        "workspace_confidence",
        "agent",
    ):
        assert field in row
    assert row["agent"] == "codex"
    assert row["total_tokens"] == 1540
    assert isinstance(row["updated_at"], str)
    assert isinstance(row["activity_at"], str)

    # Child sessions are hidden from the default History view but can be
    # explicitly requested for parent/subagent inspection.
    session.is_subagent = True
    session.parent_session_id = "parent-thread"
    await db_session.flush()
    hidden = (await client.get("/api/codex/sessions")).json()["sessions"]
    assert all(item["id"] != session.id for item in hidden)
    shown = (
        await client.get("/api/codex/sessions", params={"include_subagents": "true"})
    ).json()["sessions"]
    child = next(item for item in shown if item["id"] == session.id)
    assert child["is_subagent"] is True
    assert child["parent_session_id"] == "parent-thread"

    filtered = (
        await client.get("/api/codex/sessions", params={"agent": "stepcode"})
    ).json()
    assert filtered["sessions"] == []

    detail = (await client.get(f"/api/codex/sessions/{session.id}")).json()
    assert detail["agent"] == "codex"
