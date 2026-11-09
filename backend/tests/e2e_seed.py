"""Seed the disposable Agent Observatory CI database for Playwright."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent_observatory.config import settings
from agent_observatory.db.models import LocalDataSource
from agent_observatory.db.session import get_engine, reset_engine
from agent_observatory.services import users
from agent_observatory.workers.indexer import IndexerWorker


def _require_ci_database() -> None:
    name = settings().database_url.rsplit("/", 1)[-1].split("?", 1)[0]
    if name != "agent_observatory_ci":
        raise SystemExit(f"refusing to seed non-CI database: {name}")


async def main() -> None:
    _require_ci_database()
    root = Path(settings().home).expanduser().parent
    workspace = root / "workspace"
    sessions_root = Path(settings().sessions_root)
    workspace.mkdir(parents=True, exist_ok=True)
    sessions_root.mkdir(parents=True, exist_ok=True)

    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with factory() as db:
        await db.execute(
            text(
                "TRUNCATE TABLE memory_entries, session_skill_evidence, skill_definitions, "
                "activity_spans, evidence_records, thread_edges, thread_goals, "
                "local_data_sources, tool_calls, messages, agent_turns, "
                "agent_events, index_checkpoints, retention_runs, agent_sessions, "
                "system_commands, worker_heartbeats, workspaces, audit_logs, "
                "api_tokens, sessions, user_roles, users "
                "RESTART IDENTITY CASCADE"
            )
        )
        await users.create_user(
            db,
            username="admin",
            password="Synthetic-e2e-admin-42",
            roles=["admin", "operator"],
        )
        await users.create_user(
            db,
            username="viewer",
            password="viewerpass123",
            roles=["viewer"],
        )
        await db.commit()

    rollout = sessions_root / "2026" / "08" / "27" / "rollout-playwright.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    long_code_line = 'const longLine = "' + ("x" * 240) + '";'
    assistant_message = f"""Indexed response for the browser test.

## Rich browser evidence

### Colored hierarchy

- indexed **Markdown**
- safe inline `code`
- [safe link](https://example.com)

| Check | Value |
| --- | --- |
| markdown | colored |

$$
E = mc^2
$$

```javascript
const keyword = "playwright";
{long_code_line}
```

```python
def greet(name: str) -> str:
    return f"hello {{name}}"
```

<img src=x onerror="window.__rawHtmlExecuted = true">"""
    records = [
        {
            "timestamp": "2026-08-27T10:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": "playwright-history",
                "cwd": str(workspace),
                "model": "e2e-model",
            },
        },
        {
            "timestamp": "2026-08-27T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "playwright-turn"},
        },
        {
            "timestamp": "2026-08-27T10:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "Playwright history message",
            },
        },
        {
            "timestamp": "2026-08-27T10:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": json.dumps(
                    {"cmd": "printf 'playwright insight\\n'", "workdir": str(workspace)}
                ),
                "call_id": "playwright-call",
            },
        },
        {
            "timestamp": "2026-08-27T10:00:04Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "playwright-call",
                "output": "playwright insight\nProcess exited with code 0\n",
            },
        },
        {
            "timestamp": "2026-08-27T10:00:05Z",
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": assistant_message,
            },
        },
        {
            "timestamp": "2026-08-27T10:00:06Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 20,
                        "output_tokens": 40,
                        "reasoning_output_tokens": 5,
                        "total_tokens": 140,
                    },
                    "last_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 20,
                        "output_tokens": 40,
                        "reasoning_output_tokens": 5,
                        "total_tokens": 140,
                    },
                },
            },
        },
        {
            "timestamp": "2026-08-27T10:00:07Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "playwright-turn"},
        },
    ]
    rollout.write_text(
        "\n".join(json.dumps(row) for row in records) + "\n",
        encoding="utf-8",
    )
    memory_db = Path(settings().codex_home) / "memories_1.sqlite"
    memory_db.parent.mkdir(parents=True, exist_ok=True)
    import sqlite3

    memory_db.unlink(missing_ok=True)
    with sqlite3.connect(memory_db) as connection:
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
                "playwright-memory",
                1_777_000_000,
                "# Browser memory\n\nUse the CI database for browser tests.",
                "Browser test memory",
                "playwright-memory",
                1_777_000_001,
                1,
                1_777_000_001,
                0,
            ),
        )
        connection.commit()

    claude_memory = (
        Path("/tmp/agent-observatory-e2e/claude-projects")
        / "playwright-project"
        / "memory"
    )
    claude_memory.mkdir(parents=True, exist_ok=True)
    (claude_memory / "MEMORY.md").write_text(
        "# Claude browser memory\n\nKeep browser checks deterministic.",
        encoding="utf-8",
    )
    worker = IndexerWorker(
        factory, worker_id="e2e-seed"
    )
    await worker.scan_once()

    async with factory() as db:
        source = await db.scalar(
            select(LocalDataSource).where(
                LocalDataSource.source_kind == "codex_jsonl"
            )
        )
        if source is None:
            raise RuntimeError("rollout source was not cataloged")
        source.record_count = len(records)
        source.byte_size = rollout.stat().st_size
        source.coverage_started_at = datetime(2026, 8, 27, 10, tzinfo=UTC)
        source.coverage_ended_at = datetime(2026, 8, 27, 10, 0, 7, tzinfo=UTC)
        source.last_success_at = datetime(2026, 8, 27, 10, 0, 8, tzinfo=UTC)
        await db.commit()
    await reset_engine()


if __name__ == "__main__":
    asyncio.run(main())
