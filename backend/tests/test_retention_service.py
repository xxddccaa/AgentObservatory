"""Retention planner and executor guards.

Everything here runs against the disposable ``retention_env`` tree, so no test
can ever point the catalog at real agent data.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from agent_observatory.db.models import (
    AgentSession,
    AuditLog,
    LocalDataSource,
    RetentionRun,
)
from agent_observatory.services import retention

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def test_plan_keeps_the_recent_window_and_marks_old_files(retention_env) -> None:
    old = retention_env.make_file(
        "codex/sessions/2026/08/01/rollout-old.jsonl", age_hours=24 * 30
    )
    recent = retention_env.make_file(
        "codex/sessions/2026/09/11/rollout-new.jsonl", age_hours=2
    )

    planned = retention.plan(
        retention.Selection(keep_days=7, entry_ids=("codex.sessions",)),
        now=NOW,
    )

    paths = [item.path for item in planned.items]
    assert str(old) in paths
    assert str(recent) not in paths
    assert planned.bytes_total == 64
    # Planning is read-only.
    assert old.exists() and recent.exists()
    skipped = {(row["entry_id"], row["reason"]) for row in planned.skipped}
    assert ("codex.sessions", "in_use") in skipped


def test_minimum_retention_hours_override_the_requested_window(
    retention_env,
) -> None:
    retention_env.config.retention_min_hours = 72
    two_days_old = retention_env.make_file(
        "codex/sessions/2026/09/09/rollout-two-days.jsonl", age_hours=48
    )
    five_days_old = retention_env.make_file(
        "codex/sessions/2026/09/06/rollout-five-days.jsonl", age_hours=24 * 5
    )

    planned = retention.plan(
        retention.Selection(keep_days=1, entry_ids=("codex.sessions",)),
        now=NOW,
    )

    paths = [item.path for item in planned.items]
    assert str(five_days_old) in paths
    assert str(two_days_old) not in paths


def test_keep_days_moves_the_frontier_backwards(retention_env) -> None:
    ten_days = retention_env.make_file(
        "codex/sessions/2026/09/01/rollout-ten.jsonl", age_hours=24 * 10
    )
    forty_days = retention_env.make_file(
        "codex/sessions/2026/08/02/rollout-forty.jsonl", age_hours=24 * 40
    )

    week = retention.plan(
        retention.Selection(keep_days=7, entry_ids=("codex.sessions",)), now=NOW
    )
    month = retention.plan(
        retention.Selection(keep_days=90, entry_ids=("codex.sessions",)), now=NOW
    )

    assert str(ten_days) in [item.path for item in week.items]
    assert str(forty_days) in [item.path for item in week.items]
    assert [item.path for item in month.items] == []


def test_protected_roots_are_never_planned(retention_env) -> None:
    retention_env.make_file(
        "codex/sessions/2026/08/01/rollout-old.jsonl", age_hours=24 * 30
    )
    retention_env.config.retention_protected_roots = [
        str(retention_env.root / "codex" / "sessions")
    ]

    planned = retention.plan(
        retention.Selection(keep_days=7, entry_ids=("codex.sessions",)),
        now=NOW,
    )

    assert planned.items == []
    assert planned.skipped[0]["reason"] == "protected"
    assert planned.entries[0]["status"] == "protected"


def test_directory_entries_skip_a_subtree_with_a_recent_file(retention_env) -> None:
    old = retention_env.make_file(
        "step-cli/sessions/session-a/transcript.jsonl", age_hours=24 * 30
    )
    retention_env.make_file(
        "step-cli/sessions/session-a/state.json", age_hours=2
    )
    fresh = retention_env.make_file(
        "step-cli/sessions/session-b/transcript.jsonl", age_hours=24 * 30
    )

    planned = retention.plan(
        retention.Selection(keep_days=7, entry_ids=("stepcli.sessions",)),
        now=NOW,
    )

    paths = [item.path for item in planned.items]
    assert str(fresh.parent) in paths  # wholly old directory
    assert str(old.parent) not in paths  # contains a recent file
    assert old.exists()


@pytest.mark.anyio
async def test_overview_scan_cache_tracks_retention_runs(retention_env, db_session) -> None:
    """A purge applied by another process invalidates the cached scan."""
    retention_env.make_file(
        "codex/sessions/2026/08/01/rollout-old.jsonl", age_hours=24 * 30
    )

    def codex_entry(payload):
        return next(entry for entry in payload["entries"] if entry["id"] == "codex.sessions")

    first = await retention.overview(db_session)
    assert first["cached"] is False
    assert codex_entry(first)["bytes"] == 64
    assert codex_entry(first)["files"] == 1

    second = await retention.overview(db_session)
    assert second["cached"] is True
    assert second["generated_at"] == first["generated_at"]

    # A file that appears while the TTL is still running stays invisible: that
    # is the cache working as designed.
    retention_env.make_file(
        "codex/sessions/2026/08/02/rollout-second.jsonl", age_hours=24 * 29
    )
    assert codex_entry(await retention.overview(db_session))["files"] == 1

    # A new run (queued by the API process) must drop the cached scan, so a
    # plain page reload after a purge sees the post-cleanup reality.
    db_session.add(
        RetentionRun(
            id="ret_cache_probe",
            requested_by="admin",
            scope="raw+index",
            status="pending",
            keep_days=7,
            selection={"keep_days": 7, "entry_ids": ["codex.sessions"]},
            plan={},
            result={},
        )
    )
    await db_session.flush()

    after_request = await retention.overview(db_session)
    assert after_request["cached"] is False
    assert codex_entry(after_request)["files"] == 2
    assert after_request["generated_at"] != second["generated_at"]

    # The finished run is itself a new generation, so the fresh scan is reused.
    run = await db_session.get(RetentionRun, "ret_cache_probe")
    assert run is not None
    run.status = "applied"
    run.finished_at = datetime.now(UTC)
    await db_session.flush()

    third = await retention.overview(db_session)
    assert third["cached"] is False
    assert codex_entry(third)["files"] == 2

    fourth = await retention.overview(db_session)
    assert fourth["cached"] is True
    assert fourth["generated_at"] == third["generated_at"]


def test_selection_rejects_unknown_agents_and_bad_windows(retention_env) -> None:
    with pytest.raises(retention.RetentionError):
        retention.plan(retention.Selection(keep_days=7, agents=("nope",)), now=NOW)
    with pytest.raises(retention.RetentionError):
        retention.Selection.from_payload({"keep_days": 0})
    with pytest.raises(retention.RetentionError):
        retention.Selection.from_payload({"keep_days": 7, "entry_ids": ["../etc/passwd"]})


def test_plan_token_round_trip_and_tamper_detection(retention_env) -> None:
    selection = retention.Selection(keep_days=7, entry_ids=("codex.sessions",))
    planned = retention.plan(selection, now=NOW)
    token = retention.sign_plan(selection, planned.summary(), actor_id="user-1")

    sealed = retention.verify_plan(token)
    assert sealed["actor"] == "user-1"
    assert sealed["selection"]["entry_ids"] == ["codex.sessions"]

    with pytest.raises(retention.RetentionError):
        retention.verify_plan(token[:-2] + "aa")

    retention_env.config.retention_plan_ttl = 60
    stale = retention.sign_plan(selection, planned.summary(), actor_id="user-1")
    retention_env.retention._overview_cache = None  # noqa: SLF001 - cache reset
    import json

    raw, signature = stale.split(".")
    import base64

    payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    payload["expires_at"] = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    forged = f"{encoded}.{signature}"
    with pytest.raises(retention.RetentionError):
        retention.verify_plan(forged)


def test_delete_item_refuses_paths_outside_the_catalog_root(retention_env) -> None:
    outside = retention_env.make_file("/tmp/outside-retention.jsonl", age_hours=24 * 30)
    item = retention.PlannedItem(
        path=str(outside),
        entry_id="codex.sessions",
        agent="codex",
        category="sessions",
        risk=retention.HISTORY,
        kind="file",
        bytes=64,
        mtime=0.0,
    )
    from agent_observatory.services.retention import _delete_item

    freed, files, error = _delete_item(item)
    assert (freed, files) == (0, 0)
    assert error == "path escaped its catalog root"
    assert outside.exists()


@pytest.mark.anyio
async def test_execute_run_deletes_files_and_purges_the_index(
    retention_env, db_session
) -> None:
    old_file = retention_env.make_file(
        "codex/sessions/2026/08/01/rollout-old.jsonl", age_hours=24 * 30
    )
    kept_file = retention_env.make_file(
        "codex/sessions/2026/09/11/rollout-new.jsonl", age_hours=2
    )
    source_path = retention_env.make_file(
        "codex/logs_2.sqlite", age_hours=24 * 30
    )
    db_session.add_all(
        [
            AgentSession(
                id="session-old",
                thread_id="thread-old",
                rollout_path=str(old_file),
                rollout_real_path=str(old_file),
                source_identity="codex:old",
                source_path=str(old_file),
                source_format="jsonl",
                agent="codex",
                last_activity_at=NOW - timedelta(days=30),
            ),
            AgentSession(
                id="session-kept",
                thread_id="thread-kept",
                rollout_path=str(kept_file),
                rollout_real_path=str(kept_file),
                source_identity="codex:kept",
                source_path=str(kept_file),
                source_format="jsonl",
                agent="codex",
                last_activity_at=NOW - timedelta(hours=2),
            ),
            LocalDataSource(
                id="source-logs",
                agent="codex",
                source_kind="logs",
                source_identity="sqlite:logs",
                source_path=str(source_path),
                source_format="sqlite",
                parser_version="1",
                status="indexed",
            ),
        ]
    )
    await db_session.flush()

    selection = retention.Selection(
        keep_days=7, entry_ids=("codex.sessions", "codex.logs")
    )
    planned = retention.plan(selection, now=NOW)
    await retention.enrich_plan(db_session, planned)
    assert planned.index["sessions"] == 1
    assert planned.index["sources"] == 1

    run = await retention.create_run(
        db_session,
        selection=selection,
        plan_obj=planned,
        principal_id="admin-1",
        scope="raw+index",
    )
    await db_session.flush()

    result = await retention.execute_run(db_session, run_id=run.id, now=NOW)

    assert result["status"] == retention.RUN_APPLIED
    assert not old_file.exists()
    assert kept_file.exists()
    assert result["bytes_freed"] == 128  # session file + log database
    assert result["sessions_deleted"] == 1
    assert await db_session.get(AgentSession, "session-old") is None
    assert await db_session.get(AgentSession, "session-kept") is not None
    assert await db_session.get(LocalDataSource, "source-logs") is None
    actions = (
        await db_session.scalars(select(AuditLog.action).where(AuditLog.action == "retention.purge.applied"))
    ).all()
    assert list(actions) == ["retention.purge.applied"]


@pytest.mark.anyio
async def test_execute_refuses_when_the_plan_grew(retention_env, db_session) -> None:
    retention_env.config.retention_plan_drift_bytes = 0
    retention_env.make_file(
        "codex/sessions/2026/08/01/rollout-old.jsonl", age_hours=24 * 30
    )
    selection = retention.Selection(keep_days=7, entry_ids=("codex.sessions",))
    run = await retention.create_run(
        db_session,
        selection=selection,
        plan_obj=retention.plan(selection, now=NOW),
        principal_id="admin-1",
        scope="raw+index",
    )
    run.plan = {"totals": {"bytes": 0, "files": 0}}
    await db_session.flush()

    result = await retention.execute_run(db_session, run_id=run.id, now=NOW)

    assert result["status"] == retention.RUN_REFUSED
    assert result["error"].startswith("filesystem grew past")
    assert [item.path for item in retention.plan(selection, now=NOW).items]


@pytest.mark.anyio
async def test_execute_refuses_when_retention_is_disabled(retention_env, db_session) -> None:
    retention_env.config.retention_enabled = False
    selection = retention.Selection(keep_days=7, entry_ids=("codex.sessions",))
    run = await retention.create_run(
        db_session,
        selection=selection,
        plan_obj=retention.plan(selection, now=NOW),
        principal_id="admin-1",
        scope="raw+index",
    )
    await db_session.flush()

    result = await retention.execute_run(db_session, run_id=run.id, now=NOW)

    assert result["status"] == retention.RUN_REFUSED
