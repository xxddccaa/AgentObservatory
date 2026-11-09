from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import select

from agent_observatory.db.models import LocalDataSource, MemoryEntry
from agent_observatory.workers.indexer.memories import MemoryIndexer


def _memory_db(path: Path, *, thread_id: str, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE stage1_outputs (
                thread_id TEXT PRIMARY KEY,
                source_updated_at INTEGER NOT NULL,
                raw_memory TEXT NOT NULL,
                rollout_summary TEXT NOT NULL,
                rollout_slug TEXT,
                generated_at INTEGER NOT NULL,
                usage_count INTEGER,
                last_usage INTEGER,
                selected_for_phase2 INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute(
            "INSERT INTO stage1_outputs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                thread_id,
                1_788_320_000,
                content,
                f"Summary for {thread_id}",
                f"slug-{thread_id}",
                1_788_320_001,
                2,
                1_788_320_002,
                1,
            ),
        )
        connection.commit()


@pytest.mark.anyio
async def test_memory_indexer_reads_all_supported_sources(
    tmp_path: Path, db_session
) -> None:
    codex_home = tmp_path / "codex"
    stepcode_home = tmp_path / "stepcode"
    claude_root = tmp_path / "claude-projects"
    _memory_db(
        codex_home / "memories_1.sqlite",
        thread_id="codex-thread",
        content="Remember the release process.",
    )
    _memory_db(
        stepcode_home / "memories_1.sqlite",
        thread_id="step-thread",
        content="api_key=super-secret-value",
    )
    memory_dir = claude_root / "project-one" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "MEMORY.md").write_text("# Project memory\n\nUse the CI database.")
    topic = memory_dir / "deploy-notes.md"
    topic.write_text("# Deploy notes\n\nRun the audited restart command.")

    indexer = MemoryIndexer(
        codex_home=codex_home,
        stepcode_home=stepcode_home,
        claude_projects_root=claude_root,
    )
    results = await indexer.ingest_all(db_session)
    await db_session.flush()

    assert results == {
        "codex_memories_sqlite": 1,
        "stepcode_memories_sqlite": 1,
        "claude_memory_files": 2,
    }
    entries = list(
        (await db_session.scalars(select(MemoryEntry).order_by(MemoryEntry.agent))).all()
    )
    assert len(entries) == 4
    assert {entry.agent for entry in entries} == {"codex", "stepcode", "claude"}
    assert next(entry for entry in entries if entry.thread_id == "codex-thread").scope == "thread"
    assert next(entry for entry in entries if entry.thread_id == "step-thread").sensitivity == "sensitive"
    assert next(entry for entry in entries if entry.source_path == str(topic.resolve())).content.endswith(
        "restart command."
    )
    assert all(entry.is_present for entry in entries)

    assert await indexer.ingest_all(db_session) == {
        "codex_memories_sqlite": 0,
        "stepcode_memories_sqlite": 0,
        "claude_memory_files": 0,
    }


@pytest.mark.anyio
async def test_memory_indexer_marks_removed_files_missing(
    tmp_path: Path, db_session
) -> None:
    codex_home = tmp_path / "codex"
    stepcode_home = tmp_path / "stepcode"
    claude_root = tmp_path / "claude-projects"
    memory_dir = claude_root / "project-one" / "memory"
    memory_dir.mkdir(parents=True)
    topic = memory_dir / "topic.md"
    topic.write_text("Keep this for now.")
    indexer = MemoryIndexer(
        codex_home=codex_home,
        stepcode_home=stepcode_home,
        claude_projects_root=claude_root,
    )

    await indexer.ingest_all(db_session)
    topic.unlink()
    await indexer.ingest_all(db_session)
    entry = await db_session.scalar(select(MemoryEntry))
    assert entry is not None
    assert entry.is_present is False

    sources = list((await db_session.scalars(select(LocalDataSource))).all())
    missing = {
        source.source_kind
        for source in sources
        if source.status == "missing"
    }
    assert missing == {"codex_memories_sqlite", "stepcode_memories_sqlite"}
