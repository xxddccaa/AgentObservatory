"""The Supervisor is the only component that actually deletes data."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from agent_observatory.db.models import AuditLog, RetentionRun, SystemCommand
from agent_observatory.services import retention
from agent_observatory.supervisor import Supervisor, supervisor_instance_id

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


@pytest.mark.anyio
async def test_supervisor_applies_a_retention_purge_command(
    retention_env, _test_engine, tmp_path
) -> None:
    old_file = retention_env.make_file(
        "codex/sessions/2026/08/01/rollout-old.jsonl", age_hours=24 * 30
    )
    kept_file = retention_env.make_file(
        "codex/sessions/2026/09/11/rollout-new.jsonl", age_hours=2
    )
    home = tmp_path / "ao-home"

    factory = _test_engine
    async with factory() as db:
        selection = retention.Selection(keep_days=7, entry_ids=("codex.sessions",))
        planned = retention.plan(selection, now=NOW)
        run = await retention.create_run(
            db,
            selection=selection,
            plan_obj=planned,
            principal_id="admin-1",
            scope="raw+index",
        )
        command = SystemCommand(
            command_type="retention.purge",
            component=None,
            target_instance=supervisor_instance_id(home),
            requested_by="admin-1",
            payload={"run_id": run.id},
        )
        db.add(command)
        await db.commit()
        run_id, command_id = run.id, int(command.id)

    supervisor = Supervisor(home, specs={}, poll_interval=0.01)
    # Async seam: the sync wrapper owns its own loop, which cannot run inside
    # the test's loop.
    await supervisor._process_commands_once()  # noqa: SLF001 - documented seam

    async with factory() as db:
        command_row = await db.get(SystemCommand, command_id)
        run_row = await db.get(RetentionRun, run_id)
        assert command_row is not None and run_row is not None
        assert command_row.status == "applied"
        assert run_row.status == retention.RUN_APPLIED
        assert run_row.bytes_freed == 64
        assert run_row.files_deleted == 1
        actions = list(
            (
                await db.scalars(
                    select(AuditLog.action).where(
                        AuditLog.action.in_(
                            ("system.retention.purge.applied", "retention.purge.applied")
                        )
                    )
                )
            ).all()
        )
    assert sorted(actions) == [
        "retention.purge.applied",
        "system.retention.purge.applied",
    ]
    assert not old_file.exists()
    assert kept_file.exists()
    assert (home / "logs" / "supervisor.jsonl").exists()
