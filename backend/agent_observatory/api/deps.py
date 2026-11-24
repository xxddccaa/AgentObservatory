"""Request dependencies.

- `get_db` yields a request-scoped session, commits on success, rolls back
  on exception, always closes.

There is no login: the observatory is a single-user local tool.  Access is
limited by the listen address (loopback by default) and by the Host / Origin
checks in :mod:`agent_observatory.main`.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from agent_observatory.db.session import get_session


def _client_ip(request: Request) -> str | None:
    if request.client:
        return request.client.host
    return None


def _user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent")


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    session_ctx = get_session()
    session = await session_ctx.__anext__()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        with suppress(StopAsyncIteration):
            await session_ctx.__anext__()


# typed dependency alias for route signatures
DB = Annotated[AsyncSession, Depends(get_db)]

__all__ = [
    "get_db",
    "DB",
    "_client_ip",
    "_user_agent",
]
