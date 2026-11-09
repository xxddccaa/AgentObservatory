from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


async def _login(client, username: str, password: str) -> None:
    response = await client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text


@pytest.mark.anyio
async def test_system_status_and_logs_are_admin_only(
    client, admin_user, viewer_user, tmp_path, monkeypatch
) -> None:
    from agent_observatory.api import system as system_api

    home = tmp_path / "observatory"
    logs = home / "logs"
    logs.mkdir(parents=True)
    (logs / "api.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-08-22T12:00:00+00:00",
                "component": "api",
                "stream": "stderr",
                "message": "password=do-not-return-this",
            }
        )
        + "\n"
    )
    monkeypatch.setattr(
        system_api,
        "settings",
        lambda: SimpleNamespace(home=str(home), environment="test"),
    )

    async def readiness():
        return {
            "status": "degraded",
            "components": {
                "postgresql": {"status": "up", "healthy": True},
                "indexer": {"status": "down", "healthy": False},
            },
            "recovery_actions": ["agent-observatory restart worker"],
        }

    monkeypatch.setattr(system_api, "readiness_payload", readiness)

    await _login(client, viewer_user["username"], "viewer-password-123")
    assert (await client.get("/api/system/status")).status_code == 403

    client.cookies.clear()
    await _login(client, admin_user["username"], "admin-password-123")
    status = await client.get("/api/system/status")
    assert status.status_code == 200
    assert status.json()["components"]["indexer"]["status"] == "down"

    response = await client.get("/api/system/logs?component=api&limit=10")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["level"] == "error"
    assert "do-not-return-this" not in item["message"]
    assert "[REDACTED_SECRET]" in item["message"]


@pytest.mark.anyio
async def test_system_logs_reject_invalid_cursor(client, admin_user) -> None:
    await _login(client, admin_user["username"], "admin-password-123")
    response = await client.get("/api/system/logs?cursor=not-base64")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
