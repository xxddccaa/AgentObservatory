"""Health endpoint + database/worker probes."""
from __future__ import annotations

from httpx import ASGITransport, AsyncClient


async def test_health_reports_components_when_up() -> None:
    from sqlalchemy import text

    from agent_observatory.db.session import get_engine
    from agent_observatory.main import app

    # This probe asserts the "no worker has reported yet" shape. Indexer tests
    # write real heartbeat rows into the disposable CI database, so clear them
    # first instead of depending on test execution order.
    async with get_engine().begin() as connection:
        await connection.execute(text("DELETE FROM worker_heartbeats"))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    # Dependencies are up, but a test process has no long-running workers.
    assert body["status"] == "degraded"
    assert body["postgresql"] is True
    assert "redis" not in body
    assert "redis" not in body["components"]
    assert body["version"] == "0.1.0"
    assert body["components"]["indexer"]["status"] == "down"


async def test_liveness_does_not_require_dependencies() -> None:
    from agent_observatory.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0"}


async def test_database_ping() -> None:
    from agent_observatory.config import settings
    from agent_observatory.db.session import ping_database, reset_engine

    assert await ping_database() is True
    await reset_engine()
    assert await ping_database(settings().test_database_url) is True


async def test_health_surfaces_indexer_cycle_stats() -> None:
    """A live worker's per-cycle counters/timings are exposed for diagnosis."""
    from datetime import UTC, datetime

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import text

    from agent_observatory.db.models import WorkerHeartbeat
    from agent_observatory.db.session import get_engine
    from agent_observatory.main import app

    async with get_engine().begin() as connection:
        await connection.execute(text("DELETE FROM worker_heartbeats"))
    factory_engine = get_engine()
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(factory_engine, expire_on_commit=False)
    now = datetime.now(UTC)
    async with factory() as db:
        db.add(
            WorkerHeartbeat(
                worker_id="indexer-test-cycle",
                worker_type="indexer",
                hostname="test-host",
                pid=4242,
                status="running",
                started_at=now,
                heartbeat_at=now,
                metadata_json={
                    "last_cycle": {"changed": 2, "ms": {"cycle": 250.0}},
                    "local_scan": {"changed_sources": 1, "duration_ms": 90.0},
                },
            )
        )
        await db.commit()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            body = (await client.get("/api/health")).json()
        indexer = body["components"]["indexer"]
        assert indexer["status"] == "up"
        assert indexer["last_cycle"]["changed"] == 2
        assert indexer["local_scan"]["duration_ms"] == 90.0
    finally:
        async with get_engine().begin() as connection:
            await connection.execute(text("DELETE FROM worker_heartbeats"))


async def test_public_health_does_not_expose_private_worker_errors(db_session) -> None:
    from datetime import UTC, datetime

    from sqlalchemy import delete

    from agent_observatory.db.models import WorkerHeartbeat
    from agent_observatory.main import app

    private_error = "SYNTHETIC_PRIVATE_TOOL_OUTPUT_AND_DATABASE_PARAMETERS"
    now = datetime.now(UTC)
    await db_session.execute(delete(WorkerHeartbeat))
    db_session.add(WorkerHeartbeat(
        worker_id="private-error-test", worker_type="indexer", hostname="synthetic-host",
        pid=4242, status="degraded", last_error=private_error, started_at=now, heartbeat_at=now,
        metadata_json={"local_scan": {"error": private_error, "duration_ms": 10}},
    ))
    await db_session.commit()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for route in ("/api/health", "/api/health/ready"):
            response = await client.get(route)
            assert response.status_code == 200
            assert private_error not in response.text
            indexer = response.json()["components"]["indexer"]
            assert indexer["status"] == "degraded"
            assert "Inspect local logs" in indexer["message"]
            assert indexer["local_scan"]["duration_ms"] == 10
