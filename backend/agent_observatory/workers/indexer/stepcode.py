"""Read-only importer primitives for StepCode's SQLite session store.

StepCode persists its conversation projection in ``thread_history_*.sqlite``
files rather than Codex rollout JSONL.  This module intentionally contains no
database-model or worker integration: callers can consume the bounded
iterators and project the records into their own durable store.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class StepCodeItem:
    """One item from ``thread_items``.

    ``raw_json`` is always retained, including when the JSON is malformed.
    ``payload`` is ``None`` for malformed JSON so an importer can continue
    indexing later rows without losing the original bytes.
    """

    source_path: Path
    source_identity: str
    thread_id: str
    turn_id: str
    item_id: str
    item_type: str
    rollout_ordinal: int
    created_at_ms: int
    updated_at_ordinal: int
    raw_json: str
    payload: dict[str, Any] | list[Any] | None
    text: str | None


@dataclass(frozen=True, slots=True)
class StepCodeTurn:
    source_path: Path
    source_identity: str
    thread_id: str
    turn_id: str
    rollout_ordinal: int
    status: str
    error_json: str | None
    started_at: int | None
    completed_at: int | None
    duration_ms: int | None
    first_user_item_id: str | None
    final_agent_item_id: str | None


class StepCodeImporter:
    """Discover and stream StepCode SQLite projections without writing them."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser()

    def discover(self) -> list[Path]:
        """Return existing ``thread_history_*.sqlite`` files newest first."""
        if not self.root.exists() or not self.root.is_dir():
            return []
        paths = [
            path
            for path in self.root.rglob("thread_history_*.sqlite")
            if path.is_file()
        ]
        return sorted(paths, key=lambda path: path.stat().st_mtime_ns, reverse=True)

    @staticmethod
    def source_identity(path: str | Path, thread_id: str) -> str:
        """Build a stable, collision-resistant identity for a source thread."""
        resolved = str(Path(path).expanduser().resolve())
        digest = hashlib.sha256(f"stepcode_sqlite\0{resolved}\0{thread_id}".encode()).hexdigest()
        return f"stepcode:{digest}"

    def iter_threads(self, path: str | Path) -> Iterator[str]:
        """Yield distinct thread IDs, or nothing when the table is absent."""
        source = Path(path).expanduser()
        with self._connect(source) as connection:
            if not self._table_exists(connection, "thread_items"):
                return
            cursor = connection.execute(
                "SELECT thread_id FROM thread_items GROUP BY thread_id ORDER BY MIN(rollout_ordinal)"
            )
            for row in cursor:
                yield str(row[0])

    def iter_items(
        self,
        path: str | Path,
        *,
        thread_id: str | None = None,
        after_ordinal: int = -1,
        batch_size: int = 500,
    ) -> Iterator[StepCodeItem]:
        """Stream items ordered by rollout ordinal.

        ``after_ordinal`` is an inclusive checkpoint boundary exclusion and is
        suitable for incremental polling. SQLite ``fetchmany`` bounds memory.
        """
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        source = Path(path).expanduser()
        with self._connect(source) as connection:
            if not self._table_exists(connection, "thread_items"):
                return
            clauses = ["rollout_ordinal > ?"]
            params: list[Any] = [after_ordinal]
            if thread_id is not None:
                clauses.append("thread_id = ?")
                params.append(thread_id)
            query = (
                "SELECT thread_id, turn_id, item_id, rollout_ordinal, created_at_ms, "
                "item_json, item_type, updated_at_ordinal "
                "FROM thread_items WHERE "
                + " AND ".join(clauses)
                + " ORDER BY rollout_ordinal, thread_id, turn_id, item_id"
            )
            cursor = connection.execute(query, params)
            while rows := cursor.fetchmany(batch_size):
                for row in rows:
                    raw_json = str(row[5])
                    payload: dict[str, Any] | list[Any] | None
                    try:
                        decoded = json.loads(raw_json)
                        payload = decoded if isinstance(decoded, (dict, list)) else None
                    except (TypeError, ValueError, json.JSONDecodeError):
                        payload = None
                    yield StepCodeItem(
                        source_path=source,
                        source_identity=self.source_identity(source, str(row[0])),
                        thread_id=str(row[0]),
                        turn_id=str(row[1]),
                        item_id=str(row[2]),
                        item_type=str(row[6] or ""),
                        rollout_ordinal=int(row[3]),
                        created_at_ms=int(row[4]),
                        updated_at_ordinal=int(row[7] or 0),
                        raw_json=raw_json,
                        payload=payload,
                        text=self.extract_text(payload, str(row[6] or "")),
                    )

    def iter_turns(
        self,
        path: str | Path,
        *,
        thread_id: str | None = None,
        batch_size: int = 500,
    ) -> Iterator[StepCodeTurn]:
        """Stream turn metadata, tolerating databases from older StepCode builds."""
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        source = Path(path).expanduser()
        with self._connect(source) as connection:
            if not self._table_exists(connection, "thread_turns"):
                return
            clauses: list[str] = []
            params: list[Any] = []
            if thread_id is not None:
                clauses.append("thread_id = ?")
                params.append(thread_id)
            query = (
                "SELECT thread_id, turn_id, rollout_ordinal, status, error_json, started_at, "
                "completed_at, duration_ms, first_user_item_id, final_agent_item_id "
                "FROM thread_turns"
                + (" WHERE " + " AND ".join(clauses) if clauses else "")
                + " ORDER BY rollout_ordinal, thread_id, turn_id"
            )
            cursor = connection.execute(query, params)
            while rows := cursor.fetchmany(batch_size):
                for row in rows:
                    thread = str(row[0])
                    yield StepCodeTurn(
                        source_path=source,
                        source_identity=self.source_identity(source, thread),
                        thread_id=thread,
                        turn_id=str(row[1]),
                        rollout_ordinal=int(row[2]),
                        status=str(row[3]),
                        error_json=row[4],
                        started_at=int(row[5]) if row[5] is not None else None,
                        completed_at=int(row[6]) if row[6] is not None else None,
                        duration_ms=int(row[7]) if row[7] is not None else None,
                        first_user_item_id=row[8],
                        final_agent_item_id=row[9],
                    )

    def thread_state(self, path: str | Path, thread_id: str) -> dict[str, int]:
        """Return a small fingerprint for one thread without loading its rows."""
        source = Path(path).expanduser()
        with self._connect(source) as connection:
            if not self._table_exists(connection, "thread_items"):
                return {
                    "item_count": 0,
                    "max_rollout_ordinal": -1,
                    "max_updated_at_ordinal": -1,
                }
            row = connection.execute(
                """
                SELECT COUNT(*), COALESCE(MAX(rollout_ordinal), -1),
                       COALESCE(MAX(updated_at_ordinal), -1)
                FROM thread_items WHERE thread_id = ?
                """,
                (thread_id,),
            ).fetchone()
            assert row is not None
            return {
                "item_count": int(row[0]),
                "max_rollout_ordinal": int(row[1]),
                "max_updated_at_ordinal": int(row[2]),
            }

    def has_updated_items(
        self,
        path: str | Path,
        *,
        thread_id: str,
        through_ordinal: int,
        after_updated_ordinal: int,
    ) -> bool:
        """Detect edits to rows that were already inside a durable cursor."""
        if through_ordinal < 0:
            return False
        source = Path(path).expanduser()
        with self._connect(source) as connection:
            if not self._table_exists(connection, "thread_items"):
                return False
            row = connection.execute(
                """
                SELECT 1 FROM thread_items
                WHERE thread_id = ? AND rollout_ordinal <= ?
                  AND updated_at_ordinal > ?
                LIMIT 1
                """,
                (thread_id, through_ordinal, after_updated_ordinal),
            ).fetchone()
            return row is not None

    @staticmethod
    def extract_text(payload: object, item_type: str = "") -> str | None:
        """Extract visible text from userMessage/content-shaped payloads."""
        if not isinstance(payload, dict):
            return None
        content = payload.get("content")
        if isinstance(content, str):
            return content or None
        if not isinstance(content, list):
            return None
        parts: list[str] = []
        for entry in content:
            if (
                isinstance(entry, dict)
                and isinstance(entry.get("text"), str)
                and entry["text"]
            ):
                parts.append(entry["text"])
        text = "\n".join(parts).strip()
        return text or None

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        uri = f"file:{path.resolve()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        return row is not None


__all__ = ["StepCodeImporter", "StepCodeItem", "StepCodeTurn"]
