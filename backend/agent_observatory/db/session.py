"""Async SQLAlchemy engine + session factory.

Phase 0 keeps this minimal: the engine and a scoped async session. Model
metadata is populated in `models.py` (Phase 1). Health check uses a raw
`SELECT 1` against the engine directly so it works even before any table
exists.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from . import models  # noqa: F401  (import ensures metadata is registered)

_engine = None
_factory = None


def _db_url_for(url: str) -> str:
    """psycopg / sqlite drivers sometimes leak into async URLs; normalize."""
    return url


def get_engine(database_url: str | None = None):
    global _engine, _factory
    if _engine is not None and database_url is None:
        return _engine
    from agent_observatory.config import settings

    url = database_url or settings().database_url
    # SQLAlchemy async expects a driver prefix like postgresql+asyncpg://
    _engine = create_async_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=1800,
        future=True,
        echo=False,
    )
    _factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


async def reset_engine() -> None:
    """Tear down the cached engine (async — awaits connection cleanup).

    Required because pytest-asyncio gives each test its own event loop; a
    sync-disposed engine leaves asyncpg cleanup tasks scheduled on the old
    loop, triggering "Task was destroyed but it is pending".
    """
    global _engine, _factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _factory = None


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield a scoped async session from the shared factory.

    FastAPI's dependency injection closes the generator, which triggers the
    `finally` block. The `get_db` dependency in `api/deps.py` wraps this with
    commit/rollback; this generator only guarantees `close()`.
    """
    factory = _get_factory(get_engine())
    session = factory()
    try:
        yield session
    finally:
        await session.close()


def _get_factory(engine) -> async_sessionmaker[AsyncSession]:
    global _factory
    if _factory is None:
        _factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return _factory


async def ping_database(database_url: str | None = None) -> bool:
    """Health-check probe: cheap `SELECT 1`.

    Phase 0 uses a raw probe deliberately — `create_all` isn't called here,
    so the engine must work even with zero registered tables.
    """
    from sqlalchemy import text

    engine = get_engine(database_url)
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True
