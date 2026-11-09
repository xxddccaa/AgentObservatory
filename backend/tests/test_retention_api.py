"""Retention API: RBAC, preview contract and the command-table hand-off."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from agent_observatory.db.models import AuditLog, RetentionRun, SystemCommand


async def _login(client, username: str, password: str) -> None:
    response = await client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text


@pytest.mark.anyio
async def test_retention_requires_operator_or_admin(
    client, viewer_user, retention_env
) -> None:
    await _login(client, "viewer1", "viewer-password-123")
    overview = await client.get("/api/retention/overview")
    assert overview.status_code == 403
    preview = await client.post(
        "/api/retention/preview", json={"selection": {"keep_days": 7}}
    )
    assert preview.status_code == 403
    runs = await client.get("/api/retention/runs")
    assert runs.status_code == 403


@pytest.mark.anyio
async def test_overview_and_catalog_expose_configured_roots(
    client, as_operator, retention_env
) -> None:
    retention_env.make_file(
        "codex/sessions/2026/08/01/rollout-old.jsonl", age_hours=24 * 30
    )

    overview = await client.get("/api/retention/overview")
    assert overview.status_code == 200
    body = overview.json()
    assert body["enabled"] is True
    assert body["min_hours"] == 24
    entries = {entry["id"]: entry for entry in body["entries"]}
    assert entries["codex.sessions"]["files"] == 1
    assert entries["codex.sessions"]["bytes"] == 64

    catalog = await client.get("/api/retention/catalog")
    assert catalog.status_code == 200
    ids = {entry["id"] for entry in catalog.json()["entries"]}
    assert {"codex.sessions", "claude.sessions", "stepcli.sessions"} <= ids


@pytest.mark.anyio
async def test_api_payloads_are_never_browser_cacheable(
    client, as_operator, retention_env
) -> None:
    response = await client.get("/api/retention/overview")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.anyio
async def test_preview_then_run_queues_a_supervisor_command(
    client, admin_user, db_session, retention_env
) -> None:
    old_file = retention_env.make_file(
        "codex/sessions/2026/08/01/rollout-old.jsonl", age_hours=24 * 30
    )
    retention_env.make_file(
        "codex/sessions/2026/09/11/rollout-new.jsonl", age_hours=2
    )
    await _login(client, "admin", "admin-password-123")

    preview = await client.post(
        "/api/retention/preview",
        json={
            "selection": {
                "keep_days": 7,
                "entry_ids": ["codex.sessions"],
                "include_raw": True,
                "include_index": True,
            }
        },
    )
    assert preview.status_code == 200, preview.text
    plan = preview.json()
    assert plan["totals"]["files"] == 1
    assert plan["totals"]["bytes"] == 64
    assert plan["plan_token"]

    unconfirmed = await client.post(
        "/api/retention/runs",
        json={"plan_token": plan["plan_token"], "confirm": False},
    )
    assert unconfirmed.status_code == 400

    created = await client.post(
        "/api/retention/runs",
        json={"plan_token": plan["plan_token"], "confirm": True},
    )
    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["command_id"] > 0
    assert payload["run"]["status"] == "pending"
    assert payload["run"]["scope"] == "raw+index"

    # Nothing was deleted by the API itself: the Supervisor owns execution.
    assert old_file.exists()
    command = await db_session.get(SystemCommand, payload["command_id"])
    assert command is not None
    assert command.command_type == "retention.purge"
    assert command.payload == {"run_id": payload["run"]["id"]}
    assert command.requested_by == admin_user["id"]

    run = await db_session.get(RetentionRun, payload["run"]["id"])
    assert run is not None
    assert run.plan["totals"]["files"] == 1

    audit_actions = (
        await db_session.scalars(
            select(AuditLog.action).where(AuditLog.action == "retention.purge.requested")
        )
    ).all()
    assert list(audit_actions) == ["retention.purge.requested"]

    listed = await client.get("/api/retention/runs")
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["runs"]] == [payload["run"]["id"]]

    detail = await client.get(f"/api/retention/runs/{payload['run']['id']}")
    assert detail.status_code == 200
    assert detail.json()["command_id"] == payload["command_id"]


@pytest.mark.anyio
async def test_run_rejects_a_tampered_plan_token(
    client, as_admin, retention_env
) -> None:
    retention_env.make_file(
        "codex/sessions/2026/08/01/rollout-old.jsonl", age_hours=24 * 30
    )
    preview = await client.post(
        "/api/retention/preview",
        json={"selection": {"keep_days": 7, "entry_ids": ["codex.sessions"]}},
    )
    token = preview.json()["plan_token"]
    response = await client.post(
        "/api/retention/runs",
        json={"plan_token": token[:-3] + "abc", "confirm": True},
    )
    assert response.status_code == 400
    assert "token" in response.json()["error"]["message"]


@pytest.mark.anyio
async def test_run_is_admin_only(client, as_operator, retention_env) -> None:
    retention_env.make_file(
        "codex/sessions/2026/08/01/rollout-old.jsonl", age_hours=24 * 30
    )
    preview = await client.post(
        "/api/retention/preview",
        json={"selection": {"keep_days": 7, "entry_ids": ["codex.sessions"]}},
    )
    assert preview.status_code == 200
    response = await client.post(
        "/api/retention/runs",
        json={"plan_token": preview.json()["plan_token"], "confirm": True},
    )
    assert response.status_code == 403
