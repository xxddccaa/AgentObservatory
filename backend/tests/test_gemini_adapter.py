from __future__ import annotations

import hashlib
import json
import os
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent_observatory.db.models import (
    AgentEvent,
    AgentSession,
    AgentTurn,
    CodexMessage,
    IndexCheckpoint,
    ToolCall,
)
from agent_observatory.workers.indexer import discovery
from agent_observatory.workers.indexer.adapters import PlanAction
from agent_observatory.workers.indexer.adapters import gemini as gemini_mod
from agent_observatory.workers.indexer.adapters.codex import StepCodeImporter
from agent_observatory.workers.indexer.adapters.gemini import (
    GeminiCliSource,
    iter_records,
)
from agent_observatory.workers.indexer.orchestrator import IndexerOrchestrator
from agent_observatory.workers.indexer.scanner import RolloutScanner

SESSION_ID = "11111111-2222-4333-8444-aaaaaaaaaaaa"
SUBAGENT_ID = "subagent-99999999"


def _jsonl_lines(rows: list[dict]) -> str:
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)


def _age(*paths: Path, seconds: int = 30) -> None:
    target = datetime.now(UTC).timestamp() - seconds
    for path in paths:
        if path.exists():
            os.utime(path, (target, target))


def _home(tmp_path: Path, name: str = "gemini") -> Path:
    root = tmp_path / name
    (root / "tmp").mkdir(parents=True)
    return root


def _chat_file(
    home: Path,
    slug: str = "demo-proj",
    filename: str = "session-2026-11-24T10-00-11111111.jsonl",
    *,
    parent_dir: str | None = None,
) -> Path:
    chats = home / "tmp" / slug / "chats"
    target_dir = chats / parent_dir if parent_dir else chats
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / filename


def test_gemini_discovery_finds_main_subagents_and_legacy_json(tmp_path: Path) -> None:
    home = _home(tmp_path)
    main_jsonl = _chat_file(home, "proj-a", "session-1.jsonl")
    main_jsonl.write_text("{}\n")
    shadow_json = _chat_file(home, "proj-a", "session-1.json")
    shadow_json.write_text("{}")
    legacy_json = _chat_file(home, "proj-a", "session-2.json")
    legacy_json.write_text("{}")
    subagent_jsonl = _chat_file(
        home, "proj-a", f"{SUBAGENT_ID}.jsonl", parent_dir=SESSION_ID
    )
    subagent_jsonl.write_text("{}\n")
    # Non-chat files and internal directories must be ignored.
    (home / "tmp" / "proj-a" / "logs.json").write_text("[]")
    _chat_file(home, "proj-a", ".tmp.jsonl").write_text("{}\n")
    bg_dir = home / "tmp" / "background-processes"
    bg_dir.mkdir(parents=True)
    (bg_dir / "proc.jsonl").write_text("{}\n")

    adapter = GeminiCliSource(home)
    found = adapter.discover()
    assert [s.path for s in found] == sorted([main_jsonl, legacy_json, subagent_jsonl])
    by_path = {s.path: s for s in found}
    assert by_path[main_jsonl].format == "jsonl"
    assert by_path[legacy_json].format == "json"
    assert by_path[subagent_jsonl].agent == "gemini"
    assert adapter.layout_for(subagent_jsonl)[1] == SESSION_ID


@pytest.mark.anyio
async def test_gemini_deduplicates_snapshots_and_filters_synthetic_user_turns(
    tmp_path: Path, db_session
) -> None:
    home = _home(tmp_path)
    workspace = "/work/demo-repo"
    proj_hash = hashlib.sha256(workspace.encode()).hexdigest()
    (home / "projects.json").write_text(
        json.dumps({"projects": {workspace: "demo-proj"}})
    )
    path = _chat_file(home, "demo-proj")

    records = [
        {
            "sessionId": SESSION_ID,
            "projectHash": proj_hash,
            "startTime": "2026-11-24T01:00:00Z",
            "lastUpdated": "2026-11-24T01:05:00Z",
            "kind": "main",
        },
        {
            "id": "u-ctx",
            "timestamp": "2026-11-24T01:00:00Z",
            "type": "user",
            "content": [{"text": "<session_context>\nThis is the Gemini CLI.\n</session_context>"}],
        },
        {
            "id": "u1",
            "timestamp": "2026-11-24T01:00:01Z",
            "type": "user",
            "content": [{"text": "Fix the broken parser"}],
        },
        # Gemini CLI emits repeated snapshots for the same msg id (g1).
        {
            "id": "g1",
            "timestamp": "2026-11-24T01:00:05Z",
            "type": "gemini",
            "model": "gemini-2.5-pro",
            "content": "",
            "thoughts": [
                {"subject": "Inspecting", "description": "Checking parser logic"}
            ],
        },
        {
            "id": "g1",
            "timestamp": "2026-11-24T01:00:07Z",
            "type": "gemini",
            "model": "gemini-2.5-pro",
            "content": "",
            "thoughts": [
                {"subject": "Inspecting", "description": "Checking parser logic"}
            ],
            "toolCalls": [
                {
                    "id": "replace-1",
                    "name": "replace",
                    "args": {"file_path": "app.py", "instruction": "fix"},
                    "status": "scheduled",
                    "timestamp": "2026-11-24T01:00:06Z",
                }
            ],
            "tokens": {"input": 100, "output": 20, "cached": 10, "thoughts": 30, "tool": 0, "total": 150},
        },
        {
            "id": "g1",
            "timestamp": "2026-11-24T01:00:10Z",
            "type": "gemini",
            "model": "gemini-2.5-pro",
            "content": "Updated `app.py`.",
            "thoughts": [
                {"subject": "Inspecting", "description": "Checking parser logic"}
            ],
            "toolCalls": [
                {
                    "id": "replace-1",
                    "name": "replace",
                    "args": {"file_path": "app.py", "instruction": "fix"},
                    "result": [{"functionResponse": {"id": "replace-1", "name": "replace", "response": {"output": "Modified app.py"}}}],
                    "status": "success",
                    "timestamp": "2026-11-24T01:00:06Z",
                }
            ],
            "tokens": {"input": 100, "output": 25, "cached": 10, "thoughts": 30, "tool": 0, "total": 155},
        },
        # Synthetic tool-response user message (must NOT create a second turn/user bubble).
        {
            "id": "u-tool",
            "timestamp": "2026-11-24T01:00:11Z",
            "type": "user",
            "content": [
                {"functionResponse": {"id": "replace-1", "name": "replace", "response": {"output": "Modified app.py"}}},
                {"inlineData": {"mimeType": "image/png", "data": "AAAA"}},
            ],
        },
        {"$set": {"lastUpdated": "2026-11-24T01:00:12Z", "summary": "Fix parser bug"}},
    ]
    path.write_text(_jsonl_lines(records))

    adapter = GeminiCliSource(home)
    source = adapter.discover()[0]
    first_plan = adapter.plan(source, None)
    assert first_plan.action == PlanAction.REBUILD
    session = await adapter.ingest(db_session, first_plan)
    await db_session.flush()

    assert session.agent == "gemini"
    assert session.thread_id == SESSION_ID
    assert session.cwd == workspace
    assert session.model == "gemini-2.5-pro"
    assert session.display_title == "Fix the broken parser"
    assert session.ai_title == "Fix parser bug"
    assert session.turn_count == 1
    assert session.input_tokens == 100
    assert session.cached_input_tokens == 10
    assert session.output_tokens == 25
    assert session.reasoning_output_tokens == 30
    assert session.total_tokens == 155

    turns = (
        await db_session.scalars(
            select(AgentTurn).where(AgentTurn.agent_session_id == session.id)
        )
    ).all()
    assert len(turns) == 1
    assert turns[0].agent == "gemini"
    assert turns[0].total_tokens == 155

    events = (
        await db_session.scalars(
            select(AgentEvent)
            .where(AgentEvent.agent_session_id == session.id)
            .order_by(AgentEvent.event_index)
        )
    ).all()
    assert len(events) == len(records)
    assert all(ev.is_known for ev in events)

    msgs = (
        await db_session.scalars(
            select(CodexMessage)
            .where(CodexMessage.agent_session_id == session.id)
            .order_by(CodexMessage.sequence)
        )
    ).all()
    assert [(m.role, m.channel, m.is_visible, m.text) for m in msgs] == [
        (
            "system",
            "commentary",
            True,
            "<session_context>\nThis is the Gemini CLI.\n</session_context>",
        ),
        ("user", "final", True, "Fix the broken parser"),
        ("assistant", "reasoning", False, "**Inspecting**\nChecking parser logic"),
        ("assistant", "final", True, "Updated `app.py`."),
    ]

    tools = (
        await db_session.scalars(
            select(ToolCall).where(ToolCall.agent_session_id == session.id)
        )
    ).all()
    assert len(tools) == 1
    assert tools[0].tool_name == "replace"
    assert tools[0].tool_kind == "patch"
    assert tools[0].is_mutating is True
    assert tools[0].status == "completed"
    assert tools[0].output_text == "Modified app.py"


@pytest.mark.anyio
async def test_gemini_handles_patch_rewind_and_set_messages(
    tmp_path: Path, db_session
) -> None:
    home = _home(tmp_path)
    path = _chat_file(home, "proj-b")
    initial = [
        {"sessionId": SESSION_ID, "projectHash": "abc", "startTime": "2026-11-24T01:00:00Z"},
        {"id": "u1", "timestamp": "2026-11-24T01:00:01Z", "type": "user", "content": [{"text": "First prompt"}]},
        {
            "id": "g1",
            "timestamp": "2026-11-24T01:00:02Z",
            "type": "gemini",
            "content": "Initial answer",
            "toolCalls": [{"id": "sh-1", "name": "run_shell_command", "args": {"command": "pytest"}, "status": "success"}],
            "tokens": {"input": 10, "output": 5, "total": 15},
        },
        {"id": "u2", "timestamp": "2026-11-24T01:01:00Z", "type": "user", "content": [{"text": "Wrong follow-up"}]},
        {"id": "g2", "timestamp": "2026-11-24T01:01:05Z", "type": "gemini", "content": "Mistaken answer", "tokens": {"input": 50, "output": 20, "total": 70}},
        # Patch g1 content and tool result, then rewind away u2 and g2.
        {
            "$patch": {
                "updates": [
                    {
                        "id": "g1",
                        "content": "Patched answer",
                        "toolCalls": [{"id": "sh-1", "result": [{"functionResponse": {"response": {"output": "3 passed\nExit Code: 0"}}}]}],
                    }
                ]
            }
        },
        {"$rewindTo": "u2"},
    ]
    path.write_text(_jsonl_lines(initial))

    adapter = GeminiCliSource(home)
    source = adapter.discover()[0]
    session = await adapter.ingest(db_session, adapter.plan(source, None))
    await db_session.flush()

    msgs = (
        await db_session.scalars(
            select(CodexMessage)
            .where(CodexMessage.agent_session_id == session.id)
            .order_by(CodexMessage.sequence)
        )
    ).all()
    assert [m.text for m in msgs] == ["First prompt", "Patched answer"]
    assert session.turn_count == 1
    assert session.total_tokens == 15
    tool = await db_session.scalar(select(ToolCall).where(ToolCall.agent_session_id == session.id))
    assert tool is not None
    assert tool.exit_code == 0
    assert "3 passed" in (tool.output_text or "")

    # Appending $patch removeIds / orderIds and $set.messages replays deterministically.
    with path.open("a") as handle:
        handle.write(
            _jsonl_lines(
                [
                    {"id": "u3", "timestamp": "2026-11-24T01:02:00Z", "type": "user", "content": [{"text": "Third prompt"}]},
                    {"$patch": {"removeIds": ["g1"], "orderIds": ["u3", "u1"]}},
                ]
            )
        )
    session = await adapter.ingest(db_session, adapter.plan(source, None))
    await db_session.flush()
    reordered = (
        await db_session.scalars(
            select(CodexMessage)
            .where(CodexMessage.agent_session_id == session.id)
            .order_by(CodexMessage.sequence)
        )
    ).all()
    assert [m.text for m in reordered] == ["Third prompt", "First prompt"]

    with path.open("a") as handle:
        handle.write(
            _jsonl_lines(
                [
                    {
                        "$set": {
                            "messages": [
                                {"id": "c1", "timestamp": "2026-11-24T01:03:00Z", "type": "user", "content": [{"text": "Compacted prompt"}]},
                                {"id": "c2", "timestamp": "2026-11-24T01:03:05Z", "type": "gemini", "content": "Compacted reply", "tokens": {"input": 5, "output": 3, "total": 8}},
                            ]
                        }
                    }
                ]
            )
        )
    session = await adapter.ingest(db_session, adapter.plan(source, None))
    await db_session.flush()
    compacted = (
        await db_session.scalars(
            select(CodexMessage)
            .where(CodexMessage.agent_session_id == session.id)
            .order_by(CodexMessage.sequence)
        )
    ).all()
    assert [m.text for m in compacted] == ["Compacted prompt", "Compacted reply"]
    assert session.total_tokens == 8


@pytest.mark.anyio
async def test_gemini_subagent_and_legacy_json_fallback(
    tmp_path: Path, db_session
) -> None:
    home = _home(tmp_path)
    slug_dir = home / "tmp" / "proj-c"
    slug_dir.mkdir(parents=True)
    (slug_dir / ".project_root").write_text("/work/from-dot-root\n")

    sub_path = _chat_file(home, "proj-c", f"{SUBAGENT_ID}.jsonl", parent_dir=SESSION_ID)
    sub_path.write_text(
        _jsonl_lines(
            [
                {
                    "sessionId": SUBAGENT_ID,
                    "projectHash": "h",
                    "startTime": "2026-11-24T02:00:00Z",
                    "lastUpdated": "2026-11-24T02:01:00Z",
                    "kind": "subagent",
                    "directories": ["/work/fallback"],
                },
                {
                    "id": "su1",
                    "timestamp": "2026-11-24T02:00:02Z",
                    "type": "user",
                    "content": [{"text": "Audit module"}],
                },
            ]
        )
    )

    legacy_path = _chat_file(home, "proj-d", "session-legacy.json")
    legacy_path.write_text(
        json.dumps(
            {
                "sessionId": "legacy-sess-1",
                "projectHash": "h2",
                "startTime": "2026-11-20T00:00:00Z",
                "lastUpdated": "2026-11-20T00:05:00Z",
                "directories": ["/work/legacy-repo"],
                "messages": [
                    {"id": "lu1", "timestamp": "2026-11-20T00:00:01Z", "type": "user", "content": "Legacy question"},
                    {"id": "lg1", "timestamp": "2026-11-20T00:00:10Z", "type": "gemini", "content": "Legacy answer", "tokens": {"input": 12, "output": 8, "total": 20}},
                ],
            }
        )
    )

    adapter = GeminiCliSource(home)
    sessions = []
    for src in adapter.discover():
        sessions.append(await adapter.ingest(db_session, adapter.plan(src, None)))
    await db_session.flush()
    by_tid = {s.thread_id: s for s in sessions}

    sub = by_tid[SUBAGENT_ID]
    assert sub.parent_session_id == SESSION_ID
    assert sub.cwd == "/work/from-dot-root"
    assert sub.display_title == "Audit module"

    legacy = by_tid["legacy-sess-1"]
    assert legacy.source_format == "json"
    assert legacy.cwd == "/work/legacy-repo"
    assert legacy.total_tokens == 20
    assert legacy.turn_count == 1


@pytest.mark.anyio
async def test_gemini_bounded_memory_preserves_oversized_and_malformed_lines(
    tmp_path: Path, monkeypatch, db_session
) -> None:
    monkeypatch.setattr(gemini_mod, "MAX_LINE_BYTES", 1024)
    home = _home(tmp_path)
    path = _chat_file(home, "proj-large")
    with path.open("wb") as handle:
        handle.write(b'{"sessionId":"s-big","projectHash":"p","startTime":"2026-11-24T00:00:00Z"}\n')
        handle.write(b'{"$patch":{"huge":"' + b"x" * 2048 + b'"}}\n')
        handle.write(b"not-valid-json\n")
        handle.write(b'{"id":"m1","timestamp":"2026-11-24T00:00:05Z","type":"user","content":"After giant line"}\n')
        handle.write(b'{"id":"partial')  # incomplete final line left for next poll

    tracemalloc.start()
    try:
        records = list(iter_records(path))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 1024 * 1024
    assert [r.record_kind for r in records] == [
        "session_meta",
        "parse_error",
        "parse_error",
        "user",
    ]
    assert records[1].raw_json["parse_error"] == "line_too_large"
    assert records[2].raw_json["parse_error"] == "invalid_json"

    adapter = GeminiCliSource(home)
    session = await adapter.ingest(db_session, adapter.plan(adapter.discover()[0], None))
    await db_session.flush()
    events = (
        await db_session.scalars(
            select(AgentEvent)
            .where(AgentEvent.agent_session_id == session.id)
            .order_by(AgentEvent.event_index)
        )
    ).all()
    assert [e.is_known for e in events] == [True, False, False, True]
    assert session.display_title == "After giant line"


@pytest.mark.anyio
async def test_gemini_indexes_through_orchestrator_and_refreshes_project_meta(
    tmp_path: Path, db_session
) -> None:
    home = _home(tmp_path)
    path = _chat_file(home, "proj-orch")
    path.write_text(
        _jsonl_lines(
            [
                {"sessionId": SESSION_ID, "projectHash": "h", "startTime": "2026-11-24T03:00:00Z"},
                {"id": "u1", "timestamp": "2026-11-24T03:00:01Z", "type": "user", "content": [{"text": "Turn one"}]},
                {"id": "g1", "timestamp": "2026-11-24T03:00:05Z", "type": "gemini", "content": "Reply one", "tokens": {"input": 10, "output": 5, "total": 15}},
            ]
        )
    )
    _age(path)
    adapter = GeminiCliSource(home)
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    orchestrator = IndexerOrchestrator(
        factory,
        scanner=RolloutScanner(tmp_path / "empty-codex"),
        stepcode=StepCodeImporter(tmp_path / "empty-stepcode"),
        worker_id="indexer-gemini",
        started_at=datetime.now(UTC),
        sources=[adapter],
    )

    assert await orchestrator.scan_once() == 1
    assert await orchestrator.scan_once() == 0
    assert await orchestrator.scan_once(full=False) == 0

    # Metadata-only update via .project_root refreshes cwd even without JSONL changes.
    dot_root = home / "tmp" / "proj-orch" / ".project_root"
    dot_root.write_text("/work/updated-root\n")
    _age(dot_root)
    assert await orchestrator.scan_once(full=False) == 1
    async with factory() as check:
        session = await check.scalar(select(AgentSession).where(AgentSession.agent == "gemini"))
        assert session is not None
        assert session.cwd == "/work/updated-root"

    # Incremental JSONL append advances checkpoint cleanly.
    with path.open("a") as handle:
        handle.write(
            _jsonl_lines(
                [
                    {"id": "u2", "timestamp": "2026-11-24T03:01:00Z", "type": "user", "content": [{"text": "Turn two"}]},
                    {"id": "g2", "timestamp": "2026-11-24T03:01:05Z", "type": "gemini", "content": "Reply two", "tokens": {"input": 20, "output": 10, "total": 30}},
                ]
            )
        )
    _age(path)
    assert await orchestrator.scan_once(full=False) == 1
    async with factory() as check:
        session = await check.scalar(select(AgentSession).where(AgentSession.agent == "gemini"))
        assert session is not None
        assert session.turn_count == 2
        assert session.total_tokens == 45
        cp = await check.scalar(
            select(IndexCheckpoint).where(IndexCheckpoint.agent_session_id == session.id)
        )
        assert cp is not None
        assert cp.status == "indexed"
        assert cp.cursor_json["byte_offset"] == path.stat().st_size


def test_registry_and_discovery_support_gemini_home(tmp_path: Path, monkeypatch) -> None:
    from agent_observatory.config import Settings
    from agent_observatory.workers.indexer.registry import build_registry

    home = _home(tmp_path)
    extra = _home(tmp_path, "gemini-extra")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_OBS_GEMINI_HOME", str(home))
    monkeypatch.setenv("AGENT_OBS_EXTRA_ROOTS", f"gemini:{extra},gemini:{home}")
    config = Settings(_env_file=None)
    adapters = [a for a in build_registry(config) if a.agent == "gemini"]
    assert sorted(a.root for a in adapters) == sorted([home, extra])
    assert all(isinstance(a, GeminiCliSource) for a in adapters)
    assert config.discovery_roots["gemini"][0] == str(home / "tmp")
    assert "gemini" in discovery.IMPLEMENTED_AGENTS
