from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_observatory.db.models import (
    ActivitySpan,
    AgentSession,
    AgentTurn,
    AuditLog,
    EvidenceRecord,
    LocalDataSource,
    SessionSkillEvidence,
    SkillDefinition,
    ThreadEdge,
    ThreadGoal,
)
from agent_observatory.workers.indexer import IndexerWorker
from agent_observatory.workers.indexer.local_sources import LocalInsightsIndexer


def _create_sqlite(path: Path, statements: list[str]) -> None:
    with sqlite3.connect(path) as connection:
        for statement in statements:
            connection.execute(statement)
        connection.commit()


def _sql_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _seed_sources(home: Path, now: datetime) -> dict[str, str]:
    thread_id = "thread-1"
    child_thread_id = "thread-child"
    turn_id = "turn-1"
    now_seconds = int(now.timestamp())
    now_ms = int(now.timestamp() * 1000)
    old_seconds = int((now - timedelta(days=120)).timestamp())
    old_ms = old_seconds * 1000
    item_payload = {
        "type": "commandExecution",
        "id": "item-1",
        "command": "cat /tmp/skills/imagegen/SKILL.md",
        "status": "completed",
        "durationMs": 125,
        "aggregatedOutput": "full\x00output",
        "completedAtMs": now_ms,
    }
    item_text = json.dumps(item_payload, ensure_ascii=False)
    second_item_payload = {
        "type": "commandExecution",
        "id": "item-2",
        "command": "printf second",
        "status": "completed",
        "durationMs": 75,
        "completedAtMs": now_ms + 200,
        "exit_code": 0,
    }
    second_item_text = json.dumps(second_item_payload, ensure_ascii=False)
    _create_sqlite(
        home / "thread_history_1.sqlite",
        [
            """
            CREATE TABLE thread_turns (
                thread_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                rollout_ordinal INTEGER NOT NULL,
                status TEXT NOT NULL,
                error_json TEXT,
                started_at INTEGER,
                completed_at INTEGER,
                duration_ms INTEGER,
                PRIMARY KEY (thread_id, turn_id)
            )
            """,
            """
            CREATE TABLE thread_items (
                thread_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                rollout_ordinal INTEGER NOT NULL,
                created_at_ms INTEGER NOT NULL,
                item_json TEXT NOT NULL,
                item_type TEXT NOT NULL,
                updated_at_ordinal INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (thread_id, turn_id, item_id)
            )
            """,
            (
                "INSERT INTO thread_turns VALUES "
                f"('{thread_id}', '{turn_id}', 1, 'completed', NULL, "
                f"{now_seconds - 2}, {now_seconds}, 2000)"
            ),
            (
                "INSERT INTO thread_turns VALUES "
                f"('old-thread', 'old-turn', 1, 'completed', NULL, "
                f"{old_seconds - 2}, {old_seconds}, 2000)"
            ),
            (
                "INSERT INTO thread_items VALUES "
                f"('{thread_id}', '{turn_id}', 'item-1', 2, {now_ms}, "
                f"{_sql_text(item_text)}, 'commandExecution', 2)"
            ),
            (
                "INSERT INTO thread_items VALUES "
                f"('{thread_id}', '{turn_id}', 'item-2', 3, {now_ms + 200}, "
                f"{_sql_text(second_item_text)}, 'commandExecution', 2)"
            ),
            (
                "INSERT INTO thread_items VALUES "
                f"('old-thread', 'old-turn', 'old-item', 2, {old_ms}, "
                f"'{{\"type\":\"reasoning\"}}', 'reasoning', 2)"
            ),
            (
                "INSERT INTO thread_items VALUES "
                f"('long-thread', 'long-turn', 'long-item', 3, {now_ms}, "
                f"'{{\"type\":\"reasoning\"}}', 'reasoning', 100)"
            ),
        ],
    )
    _create_sqlite(
        home / "state_5.sqlite",
        [
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                rollout_path TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                source TEXT NOT NULL,
                model_provider TEXT NOT NULL,
                cwd TEXT NOT NULL,
                title TEXT NOT NULL,
                approval_mode TEXT,
                archived INTEGER DEFAULT 0,
                git_branch TEXT,
                cli_version TEXT,
                model TEXT,
                name TEXT,
                created_at_ms INTEGER,
                updated_at_ms INTEGER
            )
            """,
            """
            CREATE TABLE thread_spawn_edges (
                parent_thread_id TEXT NOT NULL,
                child_thread_id TEXT NOT NULL PRIMARY KEY,
                status TEXT NOT NULL
            )
            """,
            (
                "INSERT INTO threads VALUES "
                f"('{thread_id}', '/tmp/rollout.jsonl', {old_seconds}, {now_seconds}, "
                "'cli', 'openai', '/workspace', 'Thread title', 'never', 0, "
                "'main', '0.149', 'gpt-test', 'named thread', "
                f"{old_ms}, {now_ms})"
            ),
            (
                "INSERT INTO threads VALUES "
                f"('{child_thread_id}', '/tmp/child.jsonl', {now_seconds}, {now_seconds}, "
                "'subagent', 'openai', '/workspace', 'Child', 'never', 0, "
                f"'main', '0.149', 'gpt-test', NULL, {now_ms}, {now_ms})"
            ),
            (
                "INSERT INTO thread_spawn_edges VALUES "
                f"('{thread_id}', '{child_thread_id}', 'closed')"
            ),
        ],
    )
    log_body = (
        'turn.id=turn-1 selected_entries=["imagegen"] full log body\x00tail'
    )
    _create_sqlite(
        home / "logs_2.sqlite",
        [
            """
            CREATE TABLE logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                ts_nanos INTEGER NOT NULL,
                level TEXT NOT NULL,
                target TEXT NOT NULL,
                feedback_log_body TEXT,
                module_path TEXT,
                file TEXT,
                line INTEGER,
                thread_id TEXT,
                process_uuid TEXT,
                estimated_bytes INTEGER NOT NULL DEFAULT 0
            )
            """,
            (
                "INSERT INTO logs "
                "(id, ts, ts_nanos, level, target, feedback_log_body, thread_id) "
                f"VALUES (1, {old_seconds}, 0, 'INFO', 'old', 'old body', NULL)"
            ),
            (
                "INSERT INTO logs "
                "(id, ts, ts_nanos, level, target, feedback_log_body, thread_id) "
                f"VALUES (2, {now_seconds}, 7, 'DEBUG', "
                "'codex_skills_extension::shadow_selection_experiment', "
                f"'placeholder', '{thread_id}')"
            ),
        ],
    )
    with sqlite3.connect(home / "logs_2.sqlite") as connection:
        connection.execute(
            "UPDATE logs SET feedback_log_body=? WHERE id=2", (log_body,)
        )
        connection.commit()
    _create_sqlite(
        home / "goals_1.sqlite",
        [
            """
            CREATE TABLE thread_goals (
                thread_id TEXT PRIMARY KEY NOT NULL,
                goal_id TEXT NOT NULL,
                objective TEXT NOT NULL,
                status TEXT NOT NULL,
                token_budget INTEGER,
                tokens_used INTEGER NOT NULL DEFAULT 0,
                time_used_seconds INTEGER NOT NULL DEFAULT 0,
                created_at_ms INTEGER NOT NULL,
                updated_at_ms INTEGER NOT NULL
            )
            """,
            (
                "INSERT INTO thread_goals VALUES "
                f"('{thread_id}', 'goal-1', 'audit local data', 'complete', "
                f"1000, 500, 20, {old_ms}, {now_ms})"
            ),
        ],
    )
    skill_path = home / "skills" / ".system" / "imagegen" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        "---\n"
        'name: "imagegen"\n'
        "description: >\n"
        "  Generate local images\n"
        "  for tests.\n"
        "---\n\n# Image generation\n",
        encoding="utf-8",
    )
    return {
        "thread_id": thread_id,
        "turn_id": turn_id,
        "item_text": item_text,
        "second_item_text": second_item_text,
        "log_body": log_body,
    }


@pytest.mark.asyncio
async def test_ingests_local_sources_read_only_and_idempotently(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    values = _seed_sources(tmp_path, now)
    session = AgentSession(
        id="session-1",
        thread_id=values["thread_id"],
        rollout_path="/tmp/rollout.jsonl",
        rollout_real_path="/tmp/rollout.jsonl",
        source_identity="/tmp/rollout.jsonl",
        source_path="/tmp/rollout.jsonl",
    )
    turn = AgentTurn(
        id="codex-turn-1",
        agent_session_id=session.id,
        turn_index=0,
        turn_id=values["turn_id"],
    )
    db_session.add_all([session, turn])
    await db_session.flush()

    source_hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in tmp_path.glob("*.sqlite")
    }
    indexer = LocalInsightsIndexer(tmp_path, backfill_days=90)
    first = await indexer.ingest_all(db_session)
    await db_session.flush()

    assert first == {
        "thread_history": 4,
        "state": 3,
        "logs": 1,
        "goals": 1,
        "skills": 1,
    }
    await db_session.refresh(turn)
    assert turn.status == "completed"
    assert turn.outcome == "completed"
    assert turn.duration_ms == 2000
    assert turn.active_tool_ms == 200
    assert turn.first_agent_latency_ms == 2000
    assert turn.metric_confidence == "derived"

    evidence = (
        await db_session.execute(select(EvidenceRecord).order_by(EvidenceRecord.category))
    ).scalars().all()
    assert all("\x00" not in (record.raw_text or "") for record in evidence)
    item_evidence = next(
        record for record in evidence if record.source_record_id.endswith("item-1")
    )
    assert item_evidence.raw_text == values["item_text"].replace("\x00", "\\u0000")
    assert item_evidence.raw_hash == hashlib.sha256(
        values["item_text"].encode()
    ).hexdigest()
    log_evidence = next(record for record in evidence if record.source_record_id == "log:2")
    assert log_evidence.raw_text == values["log_body"].replace("\x00", "\\u0000")
    assert log_evidence.raw_hash == hashlib.sha256(
        values["log_body"].encode()
    ).hexdigest()
    assert values["log_body"] not in (log_evidence.summary or "")
    assert not any(record.thread_id == "old-thread" for record in evidence)

    span = await db_session.scalar(
        select(ActivitySpan).where(ActivitySpan.source_record_id.like("%item-2"))
    )
    assert span is not None
    assert span.span_type == "tool"
    assert span.duration_ms == 75
    assert await db_session.scalar(select(func.count()).select_from(ThreadEdge)) == 1
    assert await db_session.scalar(select(func.count()).select_from(ThreadGoal)) == 1
    skill = await db_session.scalar(select(SkillDefinition))
    assert skill is not None
    assert skill.name == "imagegen"
    assert skill.description == "Generate local images for tests."
    skill_evidence = (
        await db_session.execute(
            select(SessionSkillEvidence).order_by(SessionSkillEvidence.evidence_type)
        )
    ).scalars().all()
    assert {(row.evidence_type, row.confidence) for row in skill_evidence} == {
        ("candidate", "exact"),
        ("loaded", "exact"),
    }

    counts_before = {
        model: await db_session.scalar(select(func.count()).select_from(model))
        for model in (
            EvidenceRecord,
            ActivitySpan,
            SkillDefinition,
            SessionSkillEvidence,
            ThreadEdge,
            ThreadGoal,
        )
    }
    second = await indexer.ingest_all(db_session)
    counts_after = {
        model: await db_session.scalar(select(func.count()).select_from(model))
        for model in counts_before
    }
    assert second["logs"] == 0
    assert second["thread_history"] == 0
    assert second["state"] == 0
    assert second["goals"] == 0
    assert second["skills"] == 0
    assert counts_after == counts_before
    log_source = await db_session.scalar(
        select(LocalDataSource).where(LocalDataSource.source_kind == "logs")
    )
    assert log_source is not None
    assert log_source.cursor_json == {"last_id": 2}
    assert log_source.status == "indexed"
    assert source_hashes == {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in tmp_path.glob("*.sqlite")
    }

    updated_payload = json.loads(values["second_item_text"])
    updated_payload["durationMs"] = 100
    with sqlite3.connect(tmp_path / "thread_history_1.sqlite") as connection:
        connection.execute(
            """
            UPDATE thread_items
            SET item_json = ?, updated_at_ordinal = 3
            WHERE thread_id = ? AND item_id = ?
            """,
            (
                json.dumps(updated_payload),
                values["thread_id"],
                "item-2",
            ),
        )
        connection.commit()
    third = await indexer.ingest_all(db_session)
    assert third["thread_history"] == 2
    await db_session.refresh(span)
    await db_session.refresh(turn)
    assert span.duration_ms == 100
    assert turn.active_tool_ms == 225


@pytest.mark.asyncio
async def test_malformed_source_degrades_without_blocking_other_sources(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    _create_sqlite(
        tmp_path / "logs_2.sqlite",
        ["CREATE TABLE unexpected (id INTEGER PRIMARY KEY)"],
    )
    skill_path = tmp_path / "skills" / "simple" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        "---\nname: simple\ndescription: Simple skill\n---\n", encoding="utf-8"
    )

    results = await LocalInsightsIndexer(tmp_path).ingest_all(db_session)
    sources = {
        source.source_kind: source
        for source in (await db_session.execute(select(LocalDataSource))).scalars()
    }

    assert results["logs"] == 0
    assert results["skills"] == 1
    assert sources["logs"].status == "degraded"
    assert sources["logs"].last_error_kind == "RuntimeError"
    assert sources["skills"].status == "indexed"
    assert sources["thread_history"].status == "missing"
    assert await db_session.scalar(select(func.count()).select_from(SkillDefinition)) == 1


@pytest.mark.asyncio
async def test_indexer_worker_audits_only_changed_local_sources(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    class FakeLocalIndexer:
        async def ingest_all(self, db: AsyncSession) -> dict[str, int]:
            source = await db.get(LocalDataSource, "fake-source")
            if source is None:
                db.add(
                    LocalDataSource(
                        id="fake-source",
                        source_kind="fake",
                        source_identity="fake:test",
                        source_path=str(tmp_path / "fake.sqlite"),
                        source_format="sqlite",
                        parser_version="1",
                        status="indexed",
                        record_count=1,
                        fingerprint_json={"revision": 1},
                    )
                )
                await db.flush()
            return {"fake": 1}

    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    worker = IndexerWorker(
        factory,
        sessions_root=tmp_path / "sessions",
        worker_id="indexer-local-test",
    )
    worker.local_insights = FakeLocalIndexer()  # type: ignore[assignment]

    assert await worker.scan_local_once() == 1
    assert await worker.scan_local_once() == 0
    async with factory() as check:
        rows = list(
            (
                await check.scalars(
                    select(AuditLog).where(
                        AuditLog.action == "codex.local_sources.indexed"
                    )
                )
            ).all()
        )
    assert len(rows) == 1
    assert rows[0].detail["changed_sources"] == ["fake"]
