from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent_observatory.workers.indexer.stepcode import StepCodeImporter


def _create_history(path: Path) -> None:
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
        """
    )
    connection.executemany(
        """
        INSERT INTO thread_items
        (thread_id, turn_id, item_id, rollout_ordinal, created_at_ms, item_json, item_type,
         updated_at_ordinal)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "thread-a",
                "turn-a",
                "item-user",
                1,
                1_700_000_000_000,
                json.dumps(
                    {
                        "type": "userMessage",
                        "content": [{"type": "text", "text": "Build the dashboard"}],
                    }
                ),
                "userMessage",
                1,
            ),
            (
                "thread-a",
                "turn-a",
                "item-reasoning",
                2,
                1_700_000_000_100,
                '{"type":"reasoning","summary":["working"]}',
                "reasoning",
                2,
            ),
            (
                "thread-b",
                "turn-b",
                "item-malformed",
                3,
                1_700_000_000_200,
                "{not json",
                "userMessage",
                3,
            ),
        ],
    )
    connection.execute(
        """
        INSERT INTO thread_turns
        (thread_id, turn_id, rollout_ordinal, status, error_json, started_at, completed_at,
         duration_ms, first_user_item_id, final_agent_item_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("thread-a", "turn-a", 1, "completed", None, 100, 200, 100, "item-user", "item-agent"),
    )
    connection.commit()
    connection.close()


def test_discovers_and_streams_stepcode_history_read_only(tmp_path: Path) -> None:
    root = tmp_path / ".stepcode" / "codex"
    root.mkdir(parents=True)
    path = root / "thread_history_1.sqlite"
    _create_history(path)
    before = (path.stat().st_mtime_ns, path.read_bytes())
    importer = StepCodeImporter(root)

    assert importer.discover() == [path]
    assert list(importer.iter_threads(path)) == ["thread-a", "thread-b"]
    items = list(importer.iter_items(path, batch_size=1))
    turns = list(importer.iter_turns(path, batch_size=1))

    assert [item.item_id for item in items] == ["item-user", "item-reasoning", "item-malformed"]
    assert items[0].text == "Build the dashboard"
    assert items[1].text is None
    assert items[2].payload is None
    assert items[2].raw_json == "{not json"
    assert turns[0].status == "completed"
    assert turns[0].first_user_item_id == "item-user"
    assert (path.stat().st_mtime_ns, path.read_bytes()) == before


def test_source_identity_is_stable_and_thread_scoped(tmp_path: Path) -> None:
    path = tmp_path / "thread_history_1.sqlite"
    _create_history(path)
    importer = StepCodeImporter(tmp_path)

    first_run = list(importer.iter_items(path, thread_id="thread-a"))
    second_run = list(importer.iter_items(path, thread_id="thread-a"))

    assert first_run[0].source_identity == second_run[0].source_identity
    assert first_run[0].source_identity != importer.source_identity(path, "thread-b")
    assert list(importer.iter_items(path, after_ordinal=1))[0].item_id == "item-reasoning"


def test_missing_tables_and_invalid_batch_size_are_safe(tmp_path: Path) -> None:
    path = tmp_path / "thread_history_empty.sqlite"
    sqlite3.connect(path).close()
    importer = StepCodeImporter(tmp_path)

    assert list(importer.iter_threads(path)) == []
    assert list(importer.iter_items(path)) == []
    assert list(importer.iter_turns(path)) == []
    with pytest.raises(ValueError, match="batch_size"):
        list(importer.iter_items(path, batch_size=0))


def test_thread_state_and_existing_item_update_detection(tmp_path: Path) -> None:
    path = tmp_path / "thread_history_1.sqlite"
    _create_history(path)
    importer = StepCodeImporter(tmp_path)

    assert importer.thread_state(path, "thread-a") == {
        "item_count": 2,
        "max_rollout_ordinal": 2,
        "max_updated_at_ordinal": 2,
    }
    assert not importer.has_updated_items(
        path,
        thread_id="thread-a",
        through_ordinal=2,
        after_updated_ordinal=2,
    )

    connection = sqlite3.connect(path)
    connection.execute(
        """
        UPDATE thread_items SET item_json = ?, updated_at_ordinal = 4
        WHERE thread_id = ? AND item_id = ?
        """,
        (
            json.dumps({"type": "userMessage", "content": "Updated"}),
            "thread-a",
            "item-user",
        ),
    )
    connection.commit()
    connection.close()

    assert importer.has_updated_items(
        path,
        thread_id="thread-a",
        through_ordinal=2,
        after_updated_ordinal=2,
    )
