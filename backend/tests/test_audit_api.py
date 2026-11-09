from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.anyio
async def test_audit_list_and_export(client: AsyncClient, admin_user) -> None:
    response = await client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "admin-password-123"},
    )
    assert response.status_code == 200
    response = await client.get("/api/audit", params={"limit": 10})
    assert response.status_code == 200
    assert response.json()["items"]
    response = await client.get("/api/audit/export", params={"format": "csv"})
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
