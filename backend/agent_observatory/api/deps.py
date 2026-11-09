"""Request dependencies: DB session lifecycle, current principal, RBAC.

- `get_db` yields a request-scoped session, commits on success, rolls back
  on exception, always closes.
- `current_principal` resolves the session cookie (or bearer token) to a
  `Principal`, raising 401 if missing/invalid.
- `optional_principal` returns an anonymous principal instead of 401 (for the
  login page to know whether you're already authenticated).
- `require_role(...)` is a dependency factory that enforces RBAC (403 if the
  caller lacks one of the listed roles).
"""
from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import suppress
from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_observatory.db.models import (
    ApiToken,
    User,
)
from agent_observatory.db.models import (
    Session as SessionModel,
)
from agent_observatory.db.session import get_session
from agent_observatory.services import auth

AUTH_COOKIE = "codex_obs_session"
BEARER_PREFIX = "bearer "


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


async def _principal_from_session_token(session: AsyncSession, token: str) -> auth.Principal | None:
    digest = auth.token_digest(token)
    row = await session.scalar(
        select(SessionModel).where(SessionModel.token_hash == digest)
    )
    if row is None or row.revoked_at is not None:
        return None
    import datetime as _dt

    now = _dt.datetime.now(_dt.UTC)
    if row.expires_at < now:
        return None
    user = await session.get(User, row.user_id)
    if user is None or not user.is_active:
        return None
    return auth.Principal(
        user_id=user.id,
        username=user.username,
        roles=frozenset(user.role_names),
    )


async def _principal_from_bearer(session: AsyncSession, token: str) -> auth.Principal | None:
    digest = auth.token_digest(token)
    row = await session.scalar(
        select(ApiToken).where(ApiToken.token_hash == digest)
    )
    if row is None or row.revoked_at is not None:
        return None
    import datetime as _dt

    now = _dt.datetime.now(_dt.UTC)
    if row.expires_at is not None and row.expires_at < now:
        return None
    user = await session.get(User, row.user_id)
    if user is None or not user.is_active:
        return None
    row.last_used_at = now
    return auth.Principal(
        user_id=user.id,
        username=user.username,
        roles=frozenset(user.role_names),
    )


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    if authorization.lower().startswith(BEARER_PREFIX):
        return authorization[len(BEARER_PREFIX) :].strip()
    return None


async def current_principal(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    session_cookie: Annotated[str | None, Cookie(alias=AUTH_COOKIE)] = None,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> auth.Principal:
    principal: auth.Principal | None = None
    if session_cookie:
        principal = await _principal_from_session_token(db, session_cookie)
    if principal is None:
        bearer = _extract_bearer(authorization)
        if bearer:
            principal = await _principal_from_bearer(db, bearer)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required"
        )
    request.state.principal = principal
    return principal


async def optional_principal(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    session_cookie: Annotated[str | None, Cookie(alias=AUTH_COOKIE)] = None,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> auth.Principal:
    try:
        return await current_principal(request, db, session_cookie, authorization)
    except HTTPException:
        return auth.Principal(user_id="", username="anonymous", roles=frozenset())


def require_role(*roles: str) -> Callable[..., Awaitable[auth.Principal]]:
    """Return a dependency that enforces the principal holds at least one role."""

    async def _guard(
        request: Request,
        db: Annotated[AsyncSession, Depends(get_db)],
        principal: Annotated[auth.Principal, Depends(current_principal)],
    ) -> auth.Principal:
        if not principal.has_role(*roles):
            from agent_observatory.services import audit

            await audit.record(
                db,
                action="auth.permission_denied",
                actor_id=principal.user_id,
                resource_type="http_request",
                resource_id=request.url.path,
                request_id=getattr(request.state, "request_id", None),
                ip_address=_client_ip(request),
                user_agent=_user_agent(request),
                detail={
                    "method": request.method,
                    "required_roles": sorted(roles),
                },
                result=audit.RESULT_FAILURE,
                error_kind="permission_denied",
            )
            await db.commit()
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of roles: {sorted(roles)}",
            )
        return principal

    return _guard


# typed dependency aliases for route signatures
DB = Annotated[AsyncSession, Depends(get_db)]
CurrentPrincipal = Annotated[auth.Principal, Depends(current_principal)]
AdminPrincipal = Annotated[
    auth.Principal, Depends(require_role(auth.ROLE_ADMIN))
]
OperatorPrincipal = Annotated[
    auth.Principal,
    Depends(require_role(auth.ROLE_ADMIN, auth.ROLE_OPERATOR)),
]

__all__ = [
    "get_db",
    "current_principal",
    "optional_principal",
    "require_role",
    "DB",
    "CurrentPrincipal",
    "AdminPrincipal",
    "OperatorPrincipal",
    "AUTH_COOKIE",
    "_client_ip",
    "_user_agent",
]
