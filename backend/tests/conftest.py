"""pytest configuration + CI-database enforcement + test fixtures.

Automated tests **must** target `agent_observatory_ci`. Any other database
(`agent_observatory`, `agent_observatory_test`, or any SQLite file) is rejected
so the running or experience environment is never wiped.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _is_disposable(url: str) -> bool:
    return urlparse(url).path.lstrip("/") == "agent_observatory_ci"


@pytest.fixture(autouse=True)
def _enforce_ci_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse automated runs against any non-CI PostgreSQL database."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.fail("TEST_DATABASE_URL not set — point at agent_observatory_ci.")
    if not _is_disposable(url):
        pytest.fail(
            f"automated tests must target agent_observatory_ci; got {url!r}"
        )
    monkeypatch.setenv("AGENT_OBS_DATABASE_URL", url)
    monkeypatch.setenv("TEST_DATABASE_URL", url)
    from agent_observatory.config import settings

    settings.cache_clear()


@pytest.fixture(autouse=True)
async def _reset_runtime_clients() -> None:
    """Dispose cached async singletons between tests so each test gets a fresh loop."""
    from agent_observatory.db.session import reset_engine

    try:
        yield
    finally:
        await reset_engine()


@pytest.fixture
async def _test_engine() -> async_sessionmaker[AsyncSession]:
    from agent_observatory.config import settings
    from agent_observatory.db.models import Base

    engine = create_async_engine(settings().database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        # Incubator tests follow the current metadata baseline exactly.
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                "TRUNCATE TABLE memory_entries, session_skill_evidence, "
                "skill_definitions, activity_spans, evidence_records, "
                "retention_runs, "
                "thread_edges, thread_goals, local_data_sources, "
                "tool_calls, messages, agent_turns, agent_events, "
                "stepgo_run_events, stepgo_runs, index_checkpoints, agent_sessions, "
                "system_commands, worker_heartbeats, workspaces, "
                "audit_logs, api_tokens, sessions, user_roles, users "
                "RESTART IDENTITY CASCADE"
            )
        )
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
async def db_session(_test_engine: async_sessionmaker[AsyncSession]) -> AsyncSession:
    """Async session over the CI database wrapped in an outer transaction.

    One outer transaction wraps the whole test and rolls back at teardown, so
    commits inside route handlers or seeded fixtures are visible to the
    code-under-test but never persist to the shared CI database.
    """
    session = _test_engine()
    transaction = await session.begin()
    try:
        yield session
    finally:
        if transaction.is_active:
            await transaction.rollback()
        await session.close()


@pytest.fixture
def app(monkeypatch, db_session):
    import contextlib

    from agent_observatory.api.deps import get_db
    from agent_observatory.main import create_app

    monkeypatch.setattr(
        "agent_observatory.main.lifespan",
        contextlib.asynccontextmanager(lambda _: (yield)),
    )
    application = create_app()

    async def _test_db():
        yield db_session

    application.dependency_overrides[get_db] = _test_db
    return application


@pytest.fixture
def retention_env(tmp_path, monkeypatch):
    """Point the retention catalog at a disposable tree under ``tmp_path``.

    Returns a namespace with the fake config, the agent root, the retention
    module and a ``make_file`` helper so tests never touch real agent data.
    """
    import os
    import time
    from types import SimpleNamespace

    from agent_observatory.services import retention

    agents_root = tmp_path / "agents"
    agents_root.mkdir()
    config = SimpleNamespace(
        home=str(tmp_path / "ao-home"),
        codex_home=str(agents_root / "codex"),
        stepcode_root=str(agents_root / "stepcode" / "codex"),
        stepcli_home=str(agents_root / "step-cli"),
        claude_projects_root=str(agents_root / "claude" / "projects"),
        retention_enabled=True,
        retention_min_hours=24,
        retention_protected_roots=[],
        retention_max_entries=50_000,
        retention_large_run_bytes=5 * 1024**3,
        retention_overview_ttl=60.0,
        retention_plan_ttl=1800,
        retention_plan_drift_bytes=64 * 1024**2,
        api_token_secret="retention-test-secret",
    )
    monkeypatch.setattr(retention, "settings", lambda: config)
    monkeypatch.setattr("agent_observatory.api.retention.settings", lambda: config)

    env = SimpleNamespace(config=config, root=agents_root, retention=retention)

    def make_file(relative: str, *, age_hours: float, content: bytes = b"x" * 64):
        """Create a file under the disposable agent root with a back-dated mtime."""
        path = Path(relative)
        if not path.is_absolute():
            path = agents_root / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        stamp = time.time() - age_hours * 3600
        os.utime(path, (stamp, stamp))
        return path

    env.make_file = make_file
    # The overview scan cache is a module global: reset it so a previous test
    # can never make the next one read a stale payload.
    retention._overview_cache = None  # noqa: SLF001 - cache reset
    return env


@pytest.fixture
async def client(app):
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def admin_user(db_session: AsyncSession) -> dict:
    from agent_observatory.services import users

    u = await users.create_user(
        db_session, username="admin", password="admin-password-123", roles=["admin"]
    )
    await db_session.flush()
    return users.to_public_dict(u)


@pytest.fixture
async def operator_user(db_session: AsyncSession) -> dict:
    from agent_observatory.services import users as users_svc

    u = await users_svc.create_user(
        db_session, username="operator1", password="operator-password-123", roles=["operator"]
    )
    await db_session.flush()
    return users_svc.to_public_dict(u)


@pytest.fixture
async def viewer_user(db_session: AsyncSession) -> dict:
    from agent_observatory.services import users as users_svc

    u = await users_svc.create_user(
        db_session, username="viewer1", password="viewer-password-123", roles=["viewer"]
    )
    await db_session.flush()
    return users_svc.to_public_dict(u)


@pytest.fixture
async def as_admin(client, admin_user):
    return await _login(client, admin_user["username"], "admin-password-123")


@pytest.fixture
async def as_operator(client, operator_user):
    return await _login(client, operator_user["username"], "operator-password-123")


@pytest.fixture
async def as_viewer(client, viewer_user):
    return await _login(client, viewer_user["username"], "viewer-password-123")


async def _login(client, username, password):
    r = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return client
