from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import zstandard as zstd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent_observatory.db.models import (
    AgentEvent,
    AgentSession,
    AuditLog,
    IndexCheckpoint,
    WorkerHeartbeat,
    Workspace,
)
from agent_observatory.workers.indexer import IndexerWorker, RolloutScanner
from agent_observatory.workers.indexer.linking import SessionLinker
from agent_observatory.workers.indexer.parser import parse_json_line


def _record(thread_id: str, message: str = "hello") -> list[dict]:
    return [
        {
            "timestamp": "2026-08-21T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": thread_id, "cwd": "/tmp"},
        },
        {
            "timestamp": "2026-08-21T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": message},
        },
    ]


def _write(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(item) for item in records) + "\n")


def test_parser_recognizes_legacy_and_placeholder_shapes() -> None:
    root_meta = parse_json_line('{"id":"old-thread","timestamp":"2026-08-21T00:00:00Z"}')
    assert root_meta.top_level_type == "session_meta_root"
    assert root_meta.is_known

    bare_call = parse_json_line(
        '{"call_id":"c1","name":"exec_command","arguments":"{}"}'
    )
    assert bare_call.normalized_type == "function_call"

    state = parse_json_line('{"record_type":"state","payload":{"cursor":1}}')
    assert state.normalized_type == "state_placeholder"

    partial = parse_json_line(
        '{"type":"response_item","view_mode":"summary","payload":{"type":"message"}}'
    )
    assert partial.normalized_type == "partial_view"


def test_workspace_classification_distinguishes_semantic_path_kinds(
    tmp_path: Path,
) -> None:
    """Workspace kind is derived from explicit, conservative path signals."""

    linker = SessionLinker()
    assert linker.classify_workspace("/tmp/tmpesv89c1v") == (
        "ephemeral_task",
        "high",
    )
    assert linker.classify_workspace("/tmp/pytest-of-codex/pytest-1") == (
        "test_fixture",
        "high",
    )
    assert linker.classify_workspace("/etc") == ("system_directory", "high")

    # Existing git checkout (including a nested cwd) is a persistent project.
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    nested = repo / "src" / "agent"
    nested.mkdir(parents=True)
    assert linker.classify_workspace(str(nested)) == (
        "persistent_project",
        "high",
    )

    # Missing, non-temporary paths remain visible but are not called projects.
    missing = Path("/var/tmp/agent-observatory-deleted-workspace")
    assert linker.classify_workspace(str(missing)) == ("deleted_path", "low")


@pytest.mark.anyio
async def test_worker_indexes_only_changed_files(
    tmp_path: Path, db_session
) -> None:
    root = tmp_path / "sessions"
    path = root / "2026" / "08" / "21" / "rollout-one.jsonl"
    _write(path, _record("thread-one"))
    factory = async_sessionmaker(
        bind=db_session.bind, expire_on_commit=False
    )
    worker = IndexerWorker(
        factory,
        sessions_root=root,
        worker_id="indexer-test",
        poll_interval=0.01,
    )
    assert await worker.scan_once() == 1
    assert await worker.scan_once() == 0

    with path.open("a") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-08-21T10:00:02Z",
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "message": "done"},
                }
            )
            + "\n"
        )
    assert await worker.scan_once() == 1

    async with factory() as check:
        heartbeat = await check.get(WorkerHeartbeat, "indexer-test")
        assert heartbeat is not None
    assert heartbeat.active_sessions == 1


@pytest.mark.anyio
async def test_worker_commits_checkpoint_with_events(
    tmp_path: Path, db_session
) -> None:
    root = tmp_path / "sessions"
    path = root / "rollout-checkpoint.jsonl"
    _write(path, _record("thread-checkpoint"))
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    worker = IndexerWorker(
        factory, sessions_root=root, worker_id="indexer-checkpoint"
    )

    assert await worker.scan_once() == 1
    async with factory() as check:
        session = await check.scalar(
            select(AgentSession).where(
                AgentSession.thread_id == "thread-checkpoint"
            )
        )
        assert session is not None
        checkpoint = await check.scalar(
            select(IndexCheckpoint).where(
                IndexCheckpoint.agent_session_id == session.id
            )
        )
        assert checkpoint is not None
        assert checkpoint.status == "indexed"
        assert checkpoint.cursor_json["byte_offset"] == path.stat().st_size
        first_success = checkpoint.last_success_at

    with path.open("a") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-08-21T10:00:02Z",
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "message": "done"},
                }
            )
            + "\n"
        )
    assert await worker.scan_once() == 1
    async with factory() as check:
        checkpoint = await check.scalar(select(IndexCheckpoint))
        assert checkpoint is not None
        assert checkpoint.cursor_json["byte_offset"] == path.stat().st_size
        assert checkpoint.last_success_at is not None
        assert first_success is not None
        assert checkpoint.last_success_at >= first_success


@pytest.mark.anyio
async def test_one_bad_rollout_does_not_block_other_sources(
    tmp_path: Path, db_session
) -> None:
    root = tmp_path / "sessions"
    good = root / "rollout-good.jsonl"
    bad = root / "rollout-bad.jsonl"
    _write(good, _record("thread-good"))
    bad.write_bytes(b"\xff\xfe\xfd\n")
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    worker = IndexerWorker(factory, sessions_root=root, worker_id="indexer-isolation")

    assert await worker.scan_once() == 1
    async with factory() as check:
        session = await check.scalar(
            select(AgentSession).where(AgentSession.thread_id == "thread-good")
        )
        assert session is not None
        failure = await check.scalar(
            select(AuditLog).where(
                AuditLog.action == "agent.session.index_failed"
            )
        )
        assert failure is not None
        assert failure.resource_id == RolloutScanner.source_identity(bad)


@pytest.mark.anyio
async def test_worker_backfills_title_for_unchanged_indexed_file(
    tmp_path: Path, db_session
) -> None:
    root = tmp_path / "sessions"
    path = root / "2026" / "08" / "21" / "rollout-title.jsonl"
    _write(path, _record("thread-title", "Please use my first message as the title"))
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    worker = IndexerWorker(factory, sessions_root=root, worker_id="indexer-title")

    assert await worker.scan_once() == 1
    async with factory() as setup:
        session = await setup.scalar(
            select(AgentSession).where(AgentSession.thread_id == "thread-title")
        )
        assert session is not None
        assert session.display_title == "Please use my first message as the title"
        session.display_title = None
        session.ai_title = None
        await setup.commit()

    # The rollout metadata is unchanged, so this exercises the title-only
    # backfill path rather than a full re-index.
    assert await worker.scan_once() == 0
    async with factory() as check:
        session = await check.scalar(
            select(AgentSession).where(AgentSession.thread_id == "thread-title")
        )
        assert session is not None
        assert session.display_title == "Please use my first message as the title"
        assert session.ai_title == session.display_title


@pytest.mark.anyio
async def test_indexer_persists_one_workspace_for_equivalent_cwds(
    tmp_path: Path, db_session
) -> None:
    root = tmp_path / "sessions"
    first = root / "2026" / "08" / "21" / "rollout-workspace-one.jsonl"
    second = root / "2026" / "08" / "22" / "rollout-workspace-two.jsonl"
    first_records = _record("thread-workspace-one")
    second_records = _record("thread-workspace-two")
    first_records[0]["payload"]["cwd"] = f"{tmp_path}/"
    second_records[0]["payload"]["cwd"] = str(tmp_path / "missing" / "..")
    _write(first, first_records)
    _write(second, second_records)
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)

    worker = IndexerWorker(factory, sessions_root=root, worker_id="indexer-workspace")
    assert await worker.scan_once() == 2

    async with factory() as check:
        sessions = list(
            (
                await check.scalars(
                    select(AgentSession).order_by(AgentSession.thread_id)
                )
            ).all()
        )
        workspaces = list((await check.scalars(select(Workspace))).all())
        assert len(sessions) == 2
        assert len(workspaces) == 1
        assert workspaces[0].normalized_path == str(tmp_path.resolve())
        assert {session.workspace_id for session in sessions} == {workspaces[0].id}

        # Simulate a session imported before workspace linking was enabled.
        sessions[0].workspace_id = None
        await check.commit()

    assert await worker.scan_once() == 0
    async with factory() as check:
        repaired = await check.get(AgentSession, sessions[0].id)
        assert repaired is not None
        assert repaired.workspace_id is not None


@pytest.mark.anyio
async def test_invalid_cwd_does_not_block_history_indexing(
    tmp_path: Path, db_session
) -> None:
    root = tmp_path / "sessions"
    path = root / "2026" / "08" / "21" / "rollout-invalid-cwd.jsonl"
    records = _record("thread-invalid-cwd")
    records[0]["payload"]["cwd"] = "   "
    _write(path, records)
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)

    worker = IndexerWorker(factory, sessions_root=root, worker_id="indexer-invalid-cwd")
    assert await worker.scan_once() == 1

    async with factory() as check:
        session = await check.scalar(
            select(AgentSession).where(AgentSession.thread_id == "thread-invalid-cwd")
        )
        assert session is not None
        assert session.cwd is None
        assert session.workspace_id is None
        assert await check.scalar(select(AgentEvent)) is not None


@pytest.mark.anyio
async def test_plain_to_zstd_keeps_one_session(
    tmp_path: Path, db_session
) -> None:
    root = tmp_path / "sessions"
    plain = root / "2026" / "08" / "21" / "rollout-cold.jsonl"
    records = _record("thread-cold")
    _write(plain, records)
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    worker = IndexerWorker(factory, sessions_root=root, worker_id="indexer-zstd")
    assert await worker.scan_once() == 1

    compressed = plain.with_name(plain.name + ".zst")
    compressed.write_bytes(
        zstd.ZstdCompressor().compress(
            ("\n".join(json.dumps(item) for item in records) + "\n").encode()
        )
    )
    plain.rename(plain.with_name("rollout-cold.jsonl.replaced"))
    assert await worker.scan_once() == 1

    async with factory() as check:
        sessions = list((await check.scalars(select(AgentSession))).all())
        events = list((await check.scalars(select(AgentEvent))).all())
        assert len(sessions) == 1
        assert sessions[0].rollout_compression == "zstd"
        assert sessions[0].rollout_real_path == str(compressed.resolve())
        assert len(events) == 2


@pytest.mark.anyio
async def test_same_size_rewrite_rebuilds_events(
    tmp_path: Path, db_session
) -> None:
    root = tmp_path / "sessions"
    path = root / "2026" / "08" / "21" / "rollout-rewrite.jsonl"
    first = _record("thread-rewrite", "aaaaa")
    second = _record("thread-rewrite", "bbbbb")
    _write(path, first)
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    worker = IndexerWorker(factory, sessions_root=root, worker_id="indexer-rewrite")
    assert await worker.scan_once() == 1

    _write(path, second)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert await worker.scan_once() == 1

    async with factory() as check:
        event = await check.scalar(
            select(AgentEvent).where(AgentEvent.normalized_type == "user_message")
        )
        assert event is not None
        assert event.raw_json["payload"]["message"] == "bbbbb"
