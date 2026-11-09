"""Polling-loop efficiency guarantees for the indexer worker.

These tests pin the *shape* of a cycle rather than raw wall-clock numbers:
a cycle over unchanged sources must not issue per-source queries, maintenance
work must only run when something is actually missing, and workspace
classification (which probes the filesystem) must be memoized.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_observatory.db.models import AgentSession
from agent_observatory.workers.indexer import IndexerWorker
from agent_observatory.workers.indexer.linking import SessionLinker


def _write(path: Path, thread_id: str, message: str = "hello") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
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
    path.write_text("\n".join(json.dumps(item) for item in records) + "\n")


class _StatementCounter:
    """Count statements executed on one engine while attached."""

    def __init__(self, engine) -> None:
        self.total = 0
        self.by_kind: dict[str, int] = {}
        self._engine = engine

    def _before(self, conn, cursor, statement, parameters, context, executemany):
        kind = statement.strip().split()[0].upper()
        self.total += 1
        self.by_kind[kind] = self.by_kind.get(kind, 0) + 1

    def __enter__(self) -> _StatementCounter:
        event.listen(self._engine.sync_engine, "before_cursor_execute", self._before)
        return self

    def __exit__(self, *exc_info) -> None:
        event.remove(self._engine.sync_engine, "before_cursor_execute", self._before)


@pytest.mark.anyio
async def test_warm_cycle_query_count_is_independent_of_source_count(
    tmp_path: Path, db_session: AsyncSession
) -> None:
    """A cycle with nothing to do stays within a small, fixed query budget.

    Regression guard: the loop used to fetch a checkpoint, a session row and
    sometimes a workspace per source on every poll.
    """
    root = tmp_path / "sessions"
    for index in range(30):
        _write(
            root / "2026" / "08" / "21" / f"rollout-warm-{index:03d}.jsonl",
            f"thread-warm-{index:03d}",
        )
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    worker = IndexerWorker(factory, sessions_root=root, worker_id="indexer-warm")

    assert await worker.scan_once() == 30

    with _StatementCounter(db_session.bind) as counter:
        assert await worker.scan_once() == 0

    assert counter.total <= 12, counter.by_kind
    # Only the heartbeat may write during a fully settled cycle.
    assert counter.by_kind.get("UPDATE", 0) <= 1, counter.by_kind
    assert "INSERT" not in counter.by_kind, counter.by_kind

    stats = worker.orchestrator.last_cycle
    assert stats.unchanged == 30
    assert stats.changed == 0
    assert stats.indexed == 0
    assert stats.finished is True


@pytest.mark.anyio
async def test_warm_cycle_does_not_rerun_link_or_title_backfill(
    tmp_path: Path, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Derived fields are only repaired when they are actually missing."""
    root = tmp_path / "sessions"
    _write(root / "2026" / "08" / "21" / "rollout-skip.jsonl", "thread-skip")
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    worker = IndexerWorker(factory, sessions_root=root, worker_id="indexer-skip")

    assert await worker.scan_once() == 1

    calls = {"link": 0, "title": 0}
    original_link = worker.orchestrator.linker.link_workspace
    original_title = worker.orchestrator.linker.backfill_title

    async def counting_link(db, session):
        calls["link"] += 1
        return await original_link(db, session)

    async def counting_title(db, session):
        calls["title"] += 1
        return await original_title(db, session)

    monkeypatch.setattr(worker.orchestrator.linker, "link_workspace", counting_link)
    monkeypatch.setattr(worker.orchestrator.linker, "backfill_title", counting_title)

    assert await worker.scan_once() == 0
    assert calls == {"link": 0, "title": 0}


@pytest.mark.anyio
async def test_maintenance_repairs_missing_workspace_link(
    tmp_path: Path, db_session: AsyncSession
) -> None:
    """Rows that lose their workspace link are repaired without a re-index."""
    root = tmp_path / "sessions"
    _write(root / "2026" / "08" / "21" / "rollout-repair.jsonl", "thread-repair")
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    worker = IndexerWorker(factory, sessions_root=root, worker_id="indexer-repair")

    assert await worker.scan_once() == 1
    async with factory() as db:
        session = await db.scalar(
            select(AgentSession).where(AgentSession.thread_id == "thread-repair")
        )
        assert session is not None and session.workspace_id is not None
        session.workspace_id = None
        session.display_title = None
        await db.commit()

    assert await worker.scan_once() == 0
    async with factory() as db:
        session = await db.scalar(
            select(AgentSession).where(AgentSession.thread_id == "thread-repair")
        )
        assert session is not None
        assert session.workspace_id is not None
        assert session.display_title == "hello"

    stats = worker.orchestrator.last_cycle.maintenance
    assert stats["workspace_linked"] == 1
    assert stats["titles_backfilled"] == 1


@pytest.mark.anyio
async def test_workspace_classification_is_memoized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The filesystem-probing classifier runs at most once per key per TTL."""
    SessionLinker.clear_classification_cache()
    calls = {"n": 0}
    original = SessionLinker._classify_workspace_uncached

    def counting(path: str):
        calls["n"] += 1
        return original(path)

    monkeypatch.setattr(
        SessionLinker, "_classify_workspace_uncached", classmethod(lambda cls, p: counting(p))
    )
    try:
        first = SessionLinker.classify_workspace("/tmp/some-project")
        second = SessionLinker.classify_workspace("/tmp/some-project")
        assert first == second
        assert calls["n"] == 1
    finally:
        SessionLinker.clear_classification_cache()


@pytest.mark.anyio
async def test_local_scan_cadence_is_independent_of_the_main_loop(
    tmp_path: Path, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local sources keep their own interval while the main loop is busy.

    Previously both ran in one loop, so the local-source interval silently
    stretched to the full scan cycle time.
    """
    from agent_observatory.config import settings

    root = tmp_path / "sessions"
    _write(root / "2026" / "08" / "21" / "rollout-cadence.jsonl", "thread-cadence")
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    worker = IndexerWorker(factory, sessions_root=root, worker_id="indexer-cadence")

    class _StubIndexer:
        def __init__(self) -> None:
            self.calls: list[float] = []

        async def ingest_all(self, db):
            self.calls.append(asyncio.get_running_loop().time())
            return {}

    stub = _StubIndexer()
    worker.local_insights = stub
    worker.memory_indexer = None
    monkeypatch.setattr(settings(), "local_source_poll_interval", 0.05)

    async def slow_neighbour() -> None:
        """Stand-in for a slow scan loop sharing the event loop."""
        while not worker._stop.is_set():
            await asyncio.sleep(0.4)

    local_task = asyncio.create_task(worker._local_loop())
    neighbour = asyncio.create_task(slow_neighbour())
    try:
        await asyncio.sleep(0.35)
        await worker.stop()
        await asyncio.wait_for(local_task, timeout=5)
    finally:
        neighbour.cancel()
        with suppress(asyncio.CancelledError):
            await neighbour

    assert len(stub.calls) >= 3, stub.calls
    interval = 0.05
    gaps = [later - earlier for earlier, later in zip(stub.calls, stub.calls[1:])]
    # The first iteration pays one-off warm-up (DB session, adapter setup) before it
    # reaches the ingest call, which can land the second call closer than the interval.
    # Assert the steady-state cadence: never a busy loop, never stretched to the slow
    # neighbour's 0.4s period.
    steady = gaps[1:]
    assert steady, gaps
    assert all(interval * 0.4 <= gap <= interval * 4 for gap in steady), gaps
    assert worker.orchestrator.aux_metadata["local_scan"]["changed_sources"] == 0


@pytest.mark.anyio
async def test_local_scan_failure_is_isolated_from_indexer_health(
    tmp_path: Path, db_session: AsyncSession
) -> None:
    """A failing local source must not mark the whole indexer degraded."""
    root = tmp_path / "sessions"
    _write(root / "2026" / "08" / "21" / "rollout-isolated.jsonl", "thread-isolated")
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    worker = IndexerWorker(factory, sessions_root=root, worker_id="indexer-isolated")

    class _FailingIndexer:
        async def ingest_all(self, db):
            raise RuntimeError("local source exploded")

    worker.local_insights = _FailingIndexer()
    worker.memory_indexer = None

    assert await worker.scan_local_once() == 0
    assert isinstance(worker.last_local_error, RuntimeError)
    assert worker.orchestrator.last_error is None


def _turn_records(turn_id: str, message: str, *, complete: bool = True) -> list[dict]:
    """One turn in the real Codex taxonomy (turn boundary + user + tool)."""
    records = [
        {
            "timestamp": "2026-08-21T10:00:02.000Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": turn_id},
        },
        {
            "timestamp": "2026-08-21T10:00:03.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": message},
        },
        {
            "timestamp": "2026-08-21T10:00:04.000Z",
            "type": "response_item",
            "payload": {"type": "function_call", "name": "exec_command",
                        "arguments": json.dumps({"cmd": "ls"}), "call_id": f"call_{turn_id}"},
        },
        {
            "timestamp": "2026-08-21T10:00:05.000Z",
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": f"call_{turn_id}",
                        "output": "ok\nProcess exited with code 0\n"},
        },
    ]
    if complete:
        records.append(
            {
                "timestamp": "2026-08-21T10:00:09.000Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": turn_id},
            }
        )
    return records


def _seed_rollout(path: Path, turns: list[tuple[str, str]]) -> None:
    records = [
        {
            "timestamp": "2026-08-21T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": "thread-append", "cwd": "/tmp/append"},
        }
    ]
    for turn_id, message in turns:
        records.extend(_turn_records(turn_id, message))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(item) for item in records) + "\n")


async def _projection_snapshot(db: AsyncSession, session_id: str) -> dict[str, list[str]]:
    from agent_observatory.db.models import ActivitySpan, AgentTurn, CodexMessage, ToolCall

    async def rows(model, *order):
        result = list(
            (
                await db.scalars(
                    select(model).where(model.agent_session_id == session_id).order_by(*order)
                )
            ).all()
        )
        return result

    messages = await rows(CodexMessage, CodexMessage.sequence)
    tools = await rows(ToolCall, ToolCall.tool_name, ToolCall.call_id)
    turns = await rows(AgentTurn, AgentTurn.turn_index)
    spans = await rows(ActivitySpan, ActivitySpan.source_record_id)
    return {
        "messages": [f"{m.sequence}:{m.role}:{m.text}" for m in messages],
        "tools": [f"{t.call_id}:{t.tool_name}:{t.status}" for t in tools],
        "turns": [f"{t.turn_index}:{t.status}:{t.total_tokens}" for t in turns],
        "spans": [f"{s.source_record_id}:{s.span_type}:{s.status}" for s in spans],
    }


@pytest.mark.anyio
async def test_projection_fast_path_skips_unchanged_event_set(
    tmp_path: Path, db_session: AsyncSession
) -> None:
    """Re-projecting without new events must not rebuild rows."""
    from agent_observatory.services.history import project_events

    root = tmp_path / "sessions"
    path = root / "2026" / "08" / "21" / "rollout-append.jsonl"
    _seed_rollout(path, [("turn-1", "first")])
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    worker = IndexerWorker(factory, sessions_root=root, worker_id="indexer-append")
    assert await worker.scan_once() == 1

    async with factory() as db:
        session = await db.scalar(
            select(AgentSession).where(AgentSession.thread_id == "thread-append")
        )
        assert session is not None
        assert session.projected_event_index >= 0
        before = await _projection_snapshot(db, session.id)
        assert await project_events(db, session) is False
        after = await _projection_snapshot(db, session.id)
        assert before == after

        # Re-indexing the unchanged file through the scanner must take the
        # fast path and record the skip in its rolling counters.
        await worker.scanner.index_path(db, path)
        assert await project_events(db, session) is False
    assert worker.scanner.stats["skipped"] >= 1
    assert worker.scanner.stats["projections"] >= 2


@pytest.mark.anyio
async def test_appended_turn_projection_matches_full_rebuild(
    tmp_path: Path, db_session: AsyncSession
) -> None:
    """Incremental indexing of an appended turn equals a from-scratch rebuild."""
    from agent_observatory.services.history import project_events

    root = tmp_path / "sessions"
    path = root / "2026" / "08" / "21" / "rollout-append.jsonl"
    _seed_rollout(path, [("turn-1", "first")])
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    worker = IndexerWorker(factory, sessions_root=root, worker_id="indexer-append-2")
    assert await worker.scan_once() == 1

    _seed_rollout(path, [("turn-1", "first"), ("turn-2", "second")])
    assert await worker.scan_once() == 1

    async with factory() as db:
        session = await db.scalar(
            select(AgentSession).where(AgentSession.thread_id == "thread-append")
        )
        assert session is not None
        incremental = await _projection_snapshot(db, session.id)
        # Force the full rebuild path and compare: the fast path must be a
        # pure optimisation, never a different projection.
        session.projected_event_index = -1
        assert await project_events(db, session) is True
        full = await _projection_snapshot(db, session.id)

    assert incremental == full
    assert len(incremental["turns"]) == 2


def test_delete_action_foreign_keys_are_indexed() -> None:
    """Regression guard for the O(rows x table size) delete pathology.

    Deleting a parent row runs its foreign-key action once per deleted row.
    ``ON DELETE SET NULL`` / ``CASCADE`` therefore need an index that *leads*
    with the referencing column; without one PostgreSQL sequentially scans the
    child table for every deleted parent row (one session's projection reset
    took 87s on the dev database before these indexes existed).
    """
    from sqlalchemy import PrimaryKeyConstraint, UniqueConstraint

    from agent_observatory.db.models import Base

    missing: list[str] = []
    for table in Base.metadata.sorted_tables:
        indexed_leading_columns = set()
        for candidate in list(table.indexes) + [
            constraint
            for constraint in table.constraints
            if isinstance(constraint, (PrimaryKeyConstraint, UniqueConstraint))
        ]:
            columns = list(candidate.columns)
            if columns:
                indexed_leading_columns.add(columns[0].name)
        for foreign_key in table.foreign_keys:
            if (foreign_key.ondelete or "").upper() not in {"SET NULL", "CASCADE"}:
                continue
            if foreign_key.parent.name not in indexed_leading_columns:
                missing.append(
                    f"{table.name}.{foreign_key.parent.name} "
                    f"({foreign_key.ondelete})"
                )
    assert not missing, missing


@pytest.mark.anyio
async def test_local_scan_failure_is_published_through_heartbeat(
    tmp_path: Path, db_session: AsyncSession
) -> None:
    """A failing local scan must degrade the heartbeat, not crash the worker.

    The heartbeats are JSONB columns: publishing the exception object itself
    used to raise inside the heartbeat task, which cancelled the whole worker.
    """
    root = tmp_path / "sessions"
    _write(root / "2026" / "08" / "21" / "rollout-beat.jsonl", "thread-beat")
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    worker = IndexerWorker(factory, sessions_root=root, worker_id="indexer-beat")

    class _FailingIndexer:
        async def ingest_all(self, db):
            raise RuntimeError("local source exploded")

    worker.local_insights = _FailingIndexer()
    worker.memory_indexer = None
    task = asyncio.create_task(worker._local_loop())
    try:
        for _ in range(200):
            if "local_scan" in worker.orchestrator.aux_metadata:
                break
            await asyncio.sleep(0.01)
    finally:
        await worker.stop()
        await task

    published = worker.orchestrator.aux_metadata["local_scan"]
    assert published["error"] == {
        "kind": "RuntimeError",
        "message": "local source exploded",
    }

    await worker.orchestrator.heartbeat()
    async with factory() as db:
        from agent_observatory.db.models import WorkerHeartbeat

        row = await db.get(WorkerHeartbeat, "indexer-beat")
        assert row is not None
        assert row.status == "running"
        assert row.metadata_json["local_scan"]["error"]["kind"] == "RuntimeError"


@pytest.mark.anyio
async def test_workspace_classification_cache_is_keyed_by_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Memoization is per path: one global slot would mis-classify projects."""
    SessionLinker.clear_classification_cache()
    calls: list[str] = []
    original = SessionLinker._classify_workspace_uncached

    def counting(path: str):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(
        SessionLinker,
        "_classify_workspace_uncached",
        classmethod(lambda cls, value: counting(value)),
    )
    try:
        first = SessionLinker.classify_workspace("/tmp/cache-alpha")
        second = SessionLinker.classify_workspace("/tmp/cache-beta")
        assert calls == ["/tmp/cache-alpha", "/tmp/cache-beta"]
        assert SessionLinker.classify_workspace("/tmp/cache-alpha") == first
        assert SessionLinker.classify_workspace("/tmp/cache-beta") == second
        assert calls == ["/tmp/cache-alpha", "/tmp/cache-beta"]
    finally:
        SessionLinker.clear_classification_cache()


@pytest.mark.anyio
async def test_maintenance_is_capped_and_converges(
    tmp_path: Path, db_session: AsyncSession
) -> None:
    """Title backfill works through the backlog in bounded batches."""
    root = tmp_path / "sessions"
    for index in range(5):
        _write(
            root / "2026" / "08" / "21" / f"rollout-cap-{index}.jsonl",
            f"thread-cap-{index}",
        )
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    worker = IndexerWorker(factory, sessions_root=root, worker_id="indexer-cap")
    worker.orchestrator._MAINTENANCE_BATCH = 2
    assert await worker.scan_once() == 5

    async with factory() as db:
        sessions = (await db.scalars(select(AgentSession))).all()
        for session in sessions:
            session.display_title = None
        await db.commit()

    assert (await worker.scan_once()) == 0
    assert worker.orchestrator.last_cycle.maintenance["titles_backfilled"] == 2
    assert (await worker.scan_once()) == 0
    assert worker.orchestrator.last_cycle.maintenance["titles_backfilled"] == 2
    assert (await worker.scan_once()) == 0
    assert worker.orchestrator.last_cycle.maintenance["titles_backfilled"] == 1
    assert (await worker.scan_once()) == 0
    assert worker.orchestrator.last_cycle.maintenance["titles_backfilled"] == 0

    async with factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AgentSession)
                .where(AgentSession.display_title.is_(None))
            )
        ) == 0


@pytest.mark.anyio
async def test_unrepairable_titles_are_not_rescanned(
    tmp_path: Path, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sessions without a user message can never be titled, so they must not
    occupy the maintenance batch on every cycle."""
    root = tmp_path / "sessions"
    path = root / "2026" / "08" / "21" / "rollout-notitle.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-21T10:00:00Z",
                "type": "session_meta",
                "payload": {"id": "thread-notitle", "cwd": "/tmp"},
            }
        )
        + "\n"
    )
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    worker = IndexerWorker(factory, sessions_root=root, worker_id="indexer-notitle")
    assert await worker.scan_once() == 1

    calls = {"title": 0}
    original = worker.orchestrator.linker.backfill_title

    async def counting_title(db, session):
        calls["title"] += 1
        return await original(db, session)

    monkeypatch.setattr(worker.orchestrator.linker, "backfill_title", counting_title)
    assert await worker.scan_once() == 0
    assert calls["title"] == 0


@pytest.mark.anyio
async def test_same_size_rewrite_resets_projection_cursor(
    tmp_path: Path, db_session: AsyncSession
) -> None:
    """A same-size rewrite must not serve the previous projection.

    Without the cursor reset the projection fast path would see an unchanged
    highest event index and skip, leaving the session with no messages at all
    (the reset deletes the previous projection rows).
    """
    from agent_observatory.db.models import CodexMessage

    root = tmp_path / "sessions"
    path = root / "2026" / "08" / "21" / "rollout-rewrite.jsonl"
    _seed_rollout(path, [("turn-1", "first")])
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    worker = IndexerWorker(factory, sessions_root=root, worker_id="indexer-rewrite")
    assert await worker.scan_once() == 1

    async with factory() as db:
        session = await db.scalar(
            select(AgentSession).where(AgentSession.thread_id == "thread-append")
        )
        assert session is not None
        assert session.projected_event_index >= 0
        size_before = path.stat().st_size

    # Same byte length, different content (and a newer mtime).
    _seed_rollout(path, [("turn-1", "fir5t")])
    assert path.stat().st_size == size_before
    assert await worker.scan_once() == 1

    async with factory() as db:
        session = await db.scalar(
            select(AgentSession).where(AgentSession.thread_id == "thread-append")
        )
        assert session is not None
        assert session.projected_event_index >= 0
        texts = list(
            (
                await db.scalars(
                    select(CodexMessage.text).where(
                        CodexMessage.agent_session_id == session.id,
                        CodexMessage.role == "user",
                    )
                )
            ).all()
        )
    assert "fir5t" in texts
    assert "first" not in texts


@pytest.mark.anyio
async def test_parser_version_bump_forces_rebuild(
    tmp_path: Path, db_session: AsyncSession
) -> None:
    """A parser bump must rebuild events and the projection for every session."""
    from agent_observatory.db.models import AgentEvent, CodexMessage

    root = tmp_path / "sessions"
    path = root / "2026" / "08" / "21" / "rollout-parser.jsonl"
    _seed_rollout(path, [("turn-1", "first")])
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    worker = IndexerWorker(factory, sessions_root=root, worker_id="indexer-parser")
    assert await worker.scan_once() == 1

    worker.scanner.parser_version = "99"
    assert await worker.scan_once() == 1

    async with factory() as db:
        session = await db.scalar(
            select(AgentSession).where(AgentSession.thread_id == "thread-append")
        )
        assert session is not None
        assert session.parser_version == "99"
        assert session.projected_event_index >= 0
        events = await db.scalar(
            select(func.count())
            .select_from(AgentEvent)
            .where(AgentEvent.agent_session_id == session.id)
        )
        messages = await db.scalar(
            select(func.count())
            .select_from(CodexMessage)
            .where(CodexMessage.agent_session_id == session.id)
        )
    # The reset dropped the old rows; only a real rebuild restores them.
    assert events and events > 0
    assert messages and messages > 0


@pytest.mark.anyio
async def test_evidence_batches_beyond_the_flush_boundary(
    tmp_path: Path, db_session: AsyncSession
) -> None:
    """Many evidence rows per session flush in bounded batches, none missing.

    The synthetic session crosses the 250-row evidence batch and the 1000-event
    periodic flush, then re-projects from scratch to prove batch boundaries do
    not drop rows.
    """
    from agent_observatory.db.models import CodexMessage, EvidenceRecord
    from agent_observatory.services.history import project_events

    root = tmp_path / "sessions"
    path = root / "2026" / "08" / "21" / "rollout-batch.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "timestamp": "2026-08-21T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": "thread-batch", "cwd": "/tmp"},
        }
    ]
    for index in range(320):
        records.extend(_turn_records(f"turn-{index:03d}", f"m{index:03d}"))
    path.write_text("\n".join(json.dumps(item) for item in records) + "\n")

    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    worker = IndexerWorker(factory, sessions_root=root, worker_id="indexer-batch")
    assert await worker.scan_once() == 1

    async def counts(db: AsyncSession, session_id: str) -> tuple[int | None, int | None]:
        messages = await db.scalar(
            select(func.count())
            .select_from(CodexMessage)
            .where(CodexMessage.agent_session_id == session_id)
        )
        evidence = await db.scalar(
            select(func.count())
            .select_from(EvidenceRecord)
            .where(EvidenceRecord.agent_session_id == session_id)
        )
        return messages, evidence

    async with factory() as db:
        session = await db.scalar(
            select(AgentSession).where(AgentSession.thread_id == "thread-batch")
        )
        assert session is not None
        batched = await counts(db, session.id)
        # Force the full rebuild path and compare: batching must not drop rows.
        session.projected_event_index = -1
        assert await project_events(db, session) is True
        rebuilt = await counts(db, session.id)

    assert batched == rebuilt
    assert batched[1] and batched[1] >= 320, batched


@pytest.mark.anyio
async def test_title_backfill_uses_the_first_visible_message(
    tmp_path: Path, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blank leading user message must not pin a session in the batch.

    ``backfill_title`` consumes the first *visible* user message; the
    maintenance predicate must match that, otherwise the row is re-probed on
    every cycle forever.
    """
    root = tmp_path / "sessions"
    path = root / "2026" / "08" / "21" / "rollout-visible.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "timestamp": "2026-08-21T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": "thread-visible", "cwd": "/tmp"},
        },
        {
            "timestamp": "2026-08-21T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "   \n"},
        },
        {
            "timestamp": "2026-08-21T10:00:02Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "real title"},
        },
    ]
    path.write_text("\n".join(json.dumps(item) for item in records) + "\n")
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    worker = IndexerWorker(factory, sessions_root=root, worker_id="indexer-visible")
    assert await worker.scan_once() == 1

    async with factory() as db:
        session = await db.scalar(
            select(AgentSession).where(AgentSession.thread_id == "thread-visible")
        )
        assert session is not None
        session.display_title = None
        await db.commit()

    calls = {"title": 0}
    original = worker.orchestrator.linker.backfill_title

    async def counting_title(db, session):
        calls["title"] += 1
        return await original(db, session)

    monkeypatch.setattr(worker.orchestrator.linker, "backfill_title", counting_title)
    assert await worker.scan_once() == 0
    assert calls["title"] == 1
    assert worker.orchestrator.last_cycle.maintenance["titles_backfilled"] == 1

    async with factory() as db:
        session = await db.scalar(
            select(AgentSession).where(AgentSession.thread_id == "thread-visible")
        )
        assert session is not None
        assert session.display_title == "real title"

    # Terminal: a titled session is never re-probed.
    assert await worker.scan_once() == 0
    assert calls["title"] == 1


@pytest.mark.anyio
async def test_blank_cwd_is_normalized_and_converges(
    tmp_path: Path, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blank cwd must be normalized to NULL and not re-selected forever."""
    root = tmp_path / "sessions"
    path = root / "2026" / "08" / "21" / "rollout-blankcwd.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-21T10:00:00Z",
                "type": "session_meta",
                "payload": {"id": "thread-blankcwd", "cwd": "/tmp"},
            }
        )
        + "\n"
    )
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    worker = IndexerWorker(factory, sessions_root=root, worker_id="indexer-blankcwd")
    assert await worker.scan_once() == 1

    async with factory() as db:
        session = await db.scalar(
            select(AgentSession).where(AgentSession.thread_id == "thread-blankcwd")
        )
        assert session is not None
        session.cwd = "   "
        session.workspace_id = None
        await db.commit()

    calls = {"link": 0}
    original = worker.orchestrator.linker.link_workspace

    async def counting_link(db, session):
        calls["link"] += 1
        return await original(db, session)

    monkeypatch.setattr(worker.orchestrator.linker, "link_workspace", counting_link)
    assert await worker.scan_once() == 0
    assert calls["link"] == 1
    assert worker.orchestrator.last_cycle.maintenance["cwd_normalized"] == 1

    async with factory() as db:
        session = await db.scalar(
            select(AgentSession).where(AgentSession.thread_id == "thread-blankcwd")
        )
        assert session is not None
        assert session.cwd is None

    assert await worker.scan_once() == 0
    assert calls["link"] == 1


@pytest.mark.anyio
async def test_local_loop_survives_a_failing_recovery_path(
    tmp_path: Path, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local scan whose handler also fails must not tear down the worker."""
    root = tmp_path / "sessions"
    _write(root / "2026" / "08" / "21" / "rollout-local-loop.jsonl", "thread-local")
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    worker = IndexerWorker(factory, sessions_root=root, worker_id="indexer-local-loop")

    class _Unused:
        async def ingest_all(self, db):
            return {}

    worker.local_insights = _Unused()
    worker.memory_indexer = None

    calls = {"n": 0}

    async def exploding_scan():
        calls["n"] += 1
        raise RuntimeError("database went away")

    from agent_observatory.config import settings

    monkeypatch.setattr(worker, "scan_local_once", exploding_scan)
    monkeypatch.setattr(settings(), "local_source_poll_interval", 0.05)
    task = asyncio.create_task(worker._local_loop())
    try:
        for _ in range(400):
            if calls["n"] >= 2:
                break
            await asyncio.sleep(0.01)
    finally:
        await worker.stop()
        await task

    assert calls["n"] >= 2
    assert worker.orchestrator.aux_metadata["local_scan"]["error"] == {
        "kind": "RuntimeError",
        "message": "database went away",
    }


@pytest.mark.anyio
async def test_ingest_failures_are_counted_and_degrade_the_worker(
    tmp_path: Path, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing source is counted, reported, and does not stop the cycle."""
    from agent_observatory.db.models import WorkerHeartbeat

    root = tmp_path / "sessions"
    _write(root / "2026" / "08" / "21" / "rollout-poison.jsonl", "thread-poison")
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    worker = IndexerWorker(factory, sessions_root=root, worker_id="indexer-poison")

    async def exploding_ingest(db, plan):
        raise RuntimeError("ingest exploded")

    monkeypatch.setattr(worker.orchestrator.ingestor, "ingest_codex", exploding_ingest)
    assert await worker.scan_once() == 0

    stats = worker.orchestrator.last_cycle
    assert stats.failures == 1
    assert stats.indexed == 0
    assert stats.finished is True
    assert worker.orchestrator.last_error is not None

    await worker.orchestrator.heartbeat(success=False)
    async with factory() as db:
        row = await db.get(WorkerHeartbeat, "indexer-poison")
        assert row is not None
        assert row.status == "degraded"
        assert "ingest exploded" in (row.last_error or "")
        assert row.metadata_json["last_cycle"]["failures"] == 1


@pytest.mark.anyio
async def test_scan_and_local_loops_never_write_concurrently(
    tmp_path: Path, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two loops share one write lock.

    Running them fully in parallel made PostgreSQL deadlock on the
    ``agent_sessions`` / ``agent_turns`` rows they both update (one aborted
    cycle every ~30s on the dev instance).
    """
    from agent_observatory.config import settings

    root = tmp_path / "sessions"
    _write(root / "2026" / "08" / "21" / "rollout-lock.jsonl", "thread-lock")
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    worker = IndexerWorker(factory, sessions_root=root, worker_id="indexer-lock")
    worker.poll_interval = 0.01

    class _Local:
        async def ingest_all(self, db):
            return {}

    worker.local_insights = _Local()
    worker.memory_indexer = None
    monkeypatch.setattr(settings(), "local_source_poll_interval", 0.01)

    state = {"active": 0, "max": 0, "scans": 0, "locals": 0}

    async def body(delay: float, counter: str) -> int:
        state["active"] += 1
        state["max"] = max(state["max"], state["active"])
        state[counter] += 1
        try:
            await asyncio.sleep(delay)
        finally:
            state["active"] -= 1
        return 0

    async def fake_scan():
        return await body(0.05, "scans")

    async def fake_local():
        return await body(0.02, "locals")

    monkeypatch.setattr(worker.orchestrator, "scan_once", fake_scan)
    monkeypatch.setattr(worker, "scan_local_once", fake_local)
    tasks = [
        asyncio.create_task(worker._scan_loop()),
        asyncio.create_task(worker._local_loop()),
    ]
    try:
        for _ in range(400):
            if state["scans"] >= 3 and state["locals"] >= 3:
                break
            await asyncio.sleep(0.01)
    finally:
        await worker.stop()
        for task in tasks:
            await task

    assert state["scans"] >= 3 and state["locals"] >= 3, state
    assert state["max"] == 1, state


@pytest.mark.anyio
async def test_fast_path_refreshes_mtime_derived_status(
    tmp_path: Path, db_session: AsyncSession
) -> None:
    """The projection skip must still recompute ``is_ongoing`` / ``end_time``.

    Both fields depend on the file mtime and on the current time, so a
    metadata-only touch can move them even when the event set is unchanged.
    """
    from agent_observatory.db.models import AgentTurn
    from agent_observatory.services.history import project_events

    root = tmp_path / "sessions"
    path = root / "2026" / "08" / "21" / "rollout-open.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "timestamp": "2026-08-21T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": "thread-open", "cwd": "/tmp"},
        }
    ]
    records.extend(_turn_records("turn-open", "still running", complete=False))
    path.write_text("\n".join(json.dumps(item) for item in records) + "\n")

    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    worker = IndexerWorker(factory, sessions_root=root, worker_id="indexer-open")
    assert await worker.scan_once() == 1

    async with factory() as db:
        session = await db.scalar(
            select(AgentSession).where(AgentSession.thread_id == "thread-open")
        )
        assert session is not None
        turn = await db.scalar(
            select(AgentTurn).where(AgentTurn.agent_session_id == session.id)
        )
        assert turn is not None
        turn.completed_at = None
        turn.status = "running"
        session.is_ongoing = True
        # Far outside the "recent file" window: a rebuild would say not-ongoing.
        session.file_mtime_ns = int((datetime.now(UTC).timestamp() - 3600) * 1e9)
        await db.flush()

        assert await project_events(db, session) is False
        assert session.is_ongoing is False
        assert session.end_time is not None
