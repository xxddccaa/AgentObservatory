from __future__ import annotations

import pytest
from httpx import AsyncClient

from agent_observatory.services import audit


@pytest.mark.anyio
async def test_audit_list_and_export(client: AsyncClient, db_session) -> None:
    await audit.record(db_session, action="test.action", resource_type="test")
    await db_session.flush()
    response = await client.get("/api/audit", params={"limit": 10})
    assert response.status_code == 200
    items = response.json()["items"]
    assert items
    assert items[0]["actor_type"] == "local"
    response = await client.get("/api/audit/export", params={"format": "csv"})
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
