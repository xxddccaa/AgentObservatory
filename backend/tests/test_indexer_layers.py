from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent_observatory.db.models import (
    AgentEvent,
    AgentSession,
    AgentTurn,
    IndexCheckpoint,
    LocalDataSource,
    ToolCall,
)
from agent_observatory.workers.indexer.adapters import (
    CodexJsonlSource,
    PlanAction,
    StepCodeSqliteSource,
)
from agent_observatory.workers.indexer.generic import GenericJsonlSource
from agent_observatory.workers.indexer.orchestrator import IndexerOrchestrator
from agent_observatory.workers.indexer.scanner import RolloutScanner
from agent_observatory.workers.indexer.stepcode import StepCodeImporter


class _Checkpoint:
    def __init__(
        self,
        *,
        parser_version: str = "4",
        status: str = "indexed",
        cursor_json: dict | None = None,
        fingerprint_json: dict | None = None,
    ) -> None:
        self.parser_version = parser_version
        self.status = status
        self.cursor_json = cursor_json or {}
        self.fingerprint_json = fingerprint_json or {}


def _stepcode_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE thread_items (
            thread_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            rollout_ordinal INTEGER NOT NULL,
            created_at_ms INTEGER NOT NULL,
            item_json TEXT NOT NULL,
            item_type TEXT NOT NULL DEFAULT '',
            updated_at_ordinal INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (thread_id, turn_id, item_id)
        );
        CREATE TABLE thread_turns (
            thread_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            rollout_ordinal INTEGER NOT NULL,
            status TEXT NOT NULL,
            error_json TEXT,
            started_at INTEGER,
            completed_at INTEGER,
            duration_ms INTEGER,
            first_user_item_id TEXT,
            final_agent_item_id TEXT,
            PRIMARY KEY (thread_id, turn_id)
        );
        INSERT INTO thread_items VALUES
            ('thread-a', 'turn-a', 'item-a', 1, 1000, '{}', 'userMessage', 1);
        """
    )
    connection.commit()
    connection.close()


def test_codex_adapter_uses_one_identity_for_plain_and_zstd(tmp_path: Path) -> None:
    plain = tmp_path / "rollout-demo.jsonl"
    compressed = tmp_path / "rollout-demo.jsonl.zst"
    plain.write_text(json.dumps({"type": "session_meta"}) + "\n")
    compressed.write_bytes(b"not-used-for-identity")

    assert RolloutScanner.source_identity(plain) == RolloutScanner.source_identity(
        compressed
    )
    assert CodexJsonlSource.identity(plain) == CodexJsonlSource.identity(compressed)


def test_agent_schema_uses_neutral_tables_and_foreign_keys() -> None:
    assert AgentSession.__table__.name == "agent_sessions"
    assert AgentEvent.__table__.name == "agent_events"
    assert AgentTurn.__table__.name == "agent_turns"
    assert "agent_session_id" in AgentEvent.__table__.columns
    assert "agent_session_id" in AgentTurn.__table__.columns
    assert "agent" in AgentTurn.__table__.columns
    assert "agent" in ToolCall.__table__.columns
    assert AgentSession is AgentSession
    assert AgentEvent is AgentEvent


def test_stepcode_adapter_plans_append_then_existing_update_rebuild(
    tmp_path: Path,
) -> None:
    path = tmp_path / "thread_history_1.sqlite"
    _stepcode_db(path)
    importer = StepCodeImporter(tmp_path)
    adapter = StepCodeSqliteSource(importer)
    source = adapter.discover()[0]
    initial = adapter.plan(source, None)
    assert initial.action == PlanAction.REBUILD
    checkpoint = _Checkpoint(
        cursor_json={"rollout_ordinal": 1, "updated_at_ordinal": 1},
        fingerprint_json=initial.fingerprint,
    )
    assert adapter.plan(source, checkpoint).action == PlanAction.UNCHANGED

    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO thread_items VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("thread-a", "turn-b", "item-b", 2, 2000, "{}", "assistantMessage", 2),
    )
    connection.commit()
    connection.close()
    assert adapter.plan(source, checkpoint).action == PlanAction.APPEND

    connection = sqlite3.connect(path)
    connection.execute(
        """
        UPDATE thread_items SET item_json = ?, updated_at_ordinal = ?
        WHERE thread_id = ? AND item_id = ?
        """,
        ('{"content":"changed"}', 3, "thread-a", "item-a"),
    )
    connection.commit()
    connection.close()
    assert adapter.plan(source, checkpoint).action == PlanAction.REBUILD


@pytest.mark.anyio
async def test_stepcode_orchestrator_append_and_update_rebuild(
    tmp_path: Path, db_session
) -> None:
    sqlite_root = tmp_path / "stepcode"
    sqlite_root.mkdir()
    path = sqlite_root / "thread_history_1.sqlite"
    _stepcode_db(path)
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    orchestrator = IndexerOrchestrator(
        factory,
        scanner=RolloutScanner(tmp_path / "empty-codex"),
        stepcode=StepCodeImporter(sqlite_root),
        worker_id="indexer-stepcode-layer",
        started_at=datetime.now(UTC),
    )

    assert await orchestrator.scan_once() == 1
    async with factory() as check:
        session = await check.scalar(
            select(AgentSession).where(
                AgentSession.source_kind == "stepcode_sqlite"
            )
        )
        assert session is not None
        assert session.agent == "stepcode"
        assert session.source_format == "sqlite"
        checkpoint = await check.scalar(
            select(IndexCheckpoint).where(
                IndexCheckpoint.agent_session_id == session.id
            )
        )
        assert checkpoint is not None
        assert checkpoint.agent_session_id == session.id
        assert checkpoint.cursor_json["rollout_ordinal"] == 1
        assert len(
            list(
                (
                    await check.scalars(
                        select(AgentEvent).where(
                            AgentEvent.agent_session_id == session.id
                        )
                    )
                ).all()
            )
        ) == 1

    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO thread_items VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "thread-a",
            "turn-b",
            "item-b",
            2,
            2000,
            '{"content":"second"}',
            "assistantMessage",
            2,
        ),
    )
    connection.commit()
    connection.close()
    assert await orchestrator.scan_once() == 1

    connection = sqlite3.connect(path)
    connection.execute(
        """
        UPDATE thread_items SET item_json = ?, updated_at_ordinal = ?
        WHERE thread_id = ? AND item_id = ?
        """,
        ('{"content":"rebuilt"}', 3, "thread-a", "item-a"),
    )
    connection.commit()
    connection.close()
    assert await orchestrator.scan_once() == 1

    async with factory() as check:
        events = list(
            (
                await check.scalars(
                    select(AgentEvent).order_by(AgentEvent.event_index)
                )
            ).all()
        )
        assert len(events) == 2
        assert events[0].raw_json["content"] == "rebuilt"
        assert all(event.agent_session_id == session.id for event in events)
        checkpoint = await check.scalar(select(IndexCheckpoint))
        assert checkpoint is not None
        assert checkpoint.cursor_json == {
            "rollout_ordinal": 2,
            "updated_at_ordinal": 3,
        }


@pytest.mark.anyio
async def test_codex_source_disappearing_is_isolated(
    tmp_path: Path, db_session, monkeypatch
) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    disappearing = root / "rollout-gone.jsonl"
    healthy = root / "rollout-healthy.jsonl"
    disappearing.write_text('{"type":"session_meta","payload":{"id":"gone"}}\n')
    healthy.write_text('{"type":"session_meta","payload":{"id":"healthy"}}\n')
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    orchestrator = IndexerOrchestrator(
        factory,
        scanner=RolloutScanner(root),
        stepcode=StepCodeImporter(tmp_path / "no-stepcode"),
        worker_id="indexer-race",
        started_at=datetime.now(UTC),
    )
    discovered = orchestrator.codex_source.discover()
    disappearing.unlink()
    monkeypatch.setattr(orchestrator.codex_source, "discover", lambda: discovered)

    assert await orchestrator.scan_once() == 1
    async with factory() as check:
        indexed = await check.scalar(
            select(AgentSession).where(AgentSession.thread_id == "healthy")
        )
        assert indexed is not None


@pytest.mark.anyio
async def test_registry_adapter_indexes_through_generic_orchestrator_path(
    tmp_path: Path, db_session
) -> None:
    generic_root = tmp_path / "generic"
    generic_root.mkdir()
    source_path = generic_root / "session.jsonl"
    source_path.write_text(
        '{"role":"user","text":"generic prompt"}\n'
        '{"role":"assistant","text":"generic reply"}\n'
    )
    adapter = GenericJsonlSource(generic_root, agent="generic")
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    orchestrator = IndexerOrchestrator(
        factory,
        scanner=RolloutScanner(tmp_path / "empty-codex"),
        stepcode=StepCodeImporter(tmp_path / "empty-stepcode"),
        worker_id="indexer-generic-layer",
        started_at=datetime.now(UTC),
        sources=[adapter],
    )

    assert await orchestrator.scan_once() == 1
    async with factory() as check:
        session = await check.scalar(
            select(AgentSession).where(AgentSession.agent == "generic")
        )
        assert session is not None
        checkpoint = await check.scalar(
            select(IndexCheckpoint).where(
                IndexCheckpoint.agent_session_id == session.id
            )
        )
        assert checkpoint is not None
        assert checkpoint.status == "indexed"
        source = await check.scalar(
            select(LocalDataSource).where(
                LocalDataSource.source_identity == session.source_identity
            )
        )
        assert source is not None
        assert source.agent == "generic"
        assert source.status == "indexed"
