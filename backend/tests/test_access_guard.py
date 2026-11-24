"""No login: the API is protected by its bind address plus Host / Origin checks."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from agent_observatory.main import allowed_host_names, cross_site_reason


async def _get(app, base_url: str, path: str = "/api/health/live", **kwargs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=base_url) as client:
        return await client.get(path, **kwargs)


@pytest.mark.anyio
async def test_loopback_names_are_served_without_login(app) -> None:
    for base_url in ("http://127.0.0.1", "http://localhost:8125", "http://[::1]:8125"):
        response = await _get(app, base_url)
        assert response.status_code == 200, base_url


@pytest.mark.anyio
async def test_foreign_host_is_refused_on_a_loopback_bind(app) -> None:
    # A page that rebinds its own domain to 127.0.0.1 still sends its Host.
    response = await _get(app, "http://attacker.example")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "host_not_allowed"
    assert response.headers["x-request-id"]


@pytest.mark.anyio
async def test_cross_origin_writes_are_refused(client) -> None:
    refused = await client.post(
        "/api/memories/export",
        json={"ids": ["x"]},
        headers={"Origin": "http://attacker.example"},
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "cross_origin_refused"

    fetch_site = await client.post(
        "/api/memories/export", json={"ids": ["x"]}, headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert fetch_site.status_code == 403

    # Same-origin browser requests and plain clients (no Origin) pass the guard.
    same_origin = await client.post(
        "/api/memories/export", json={"ids": []}, headers={"Origin": "http://127.0.0.1"}
    )
    assert same_origin.status_code == 422
    plain = await client.post("/api/memories/export", json={"ids": []})
    assert plain.status_code == 422

    # Reads are never refused for their Origin.
    read = await client.get("/api/health/live", headers={"Origin": "http://attacker.example"})
    assert read.status_code == 200


@pytest.mark.anyio
async def test_wildcard_bind_serves_any_host(app, monkeypatch) -> None:
    from agent_observatory.config import settings

    monkeypatch.setenv("AGENT_OBS_HOST", "0.0.0.0")
    settings.cache_clear()
    try:
        response = await _get(app, "http://192.168.1.20:8125")
        assert response.status_code == 200
    finally:
        settings.cache_clear()


def test_allowed_host_names_follow_the_bind_and_the_explicit_list() -> None:
    def config(host: str, allowed: list[str] | None = None):
        return SimpleNamespace(host=host, allowed_hosts=allowed or [])

    assert allowed_host_names(config("127.0.0.1")) == {"127.0.0.1", "localhost", "::1"}
    assert allowed_host_names(config("localhost")) == {"127.0.0.1", "localhost", "::1"}
    assert allowed_host_names(config("0.0.0.0")) is None
    assert allowed_host_names(config("::")) is None
    assert allowed_host_names(config("127.0.0.1", ["Box.Local"])) == {
        "127.0.0.1",
        "localhost",
        "::1",
        "box.local",
    }
    assert allowed_host_names(config("127.0.0.1", ["*"])) is None


def test_cross_site_reason_only_applies_to_state_changes() -> None:
    assert cross_site_reason("GET", "127.0.0.1:8125", {"origin": "http://evil"}) is None
    assert cross_site_reason("POST", "127.0.0.1:8125", {"origin": "http://127.0.0.1:8125"}) is None
    assert cross_site_reason("POST", "127.0.0.1:8125", {"origin": "http://127.0.0.1:9999"})
    assert cross_site_reason("DELETE", "127.0.0.1:8125", {"origin": "null"})
    assert cross_site_reason("POST", "127.0.0.1:8125", {}) is None
