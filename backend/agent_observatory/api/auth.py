"""Auth API: login, logout, current-user.

POST /api/auth/login   — username/password → HttpOnly session cookie
POST /api/auth/logout  — revoke session + clear cookie
GET  /api/auth/me      — current principal (requires auth)
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from agent_observatory.api.deps import (
    AUTH_COOKIE,
    DB,
    CurrentPrincipal,
    _client_ip,
    _user_agent,
)
from agent_observatory.db.models import ApiToken
from agent_observatory.db.models import Session as SessionModel
from agent_observatory.services import audit, users
from agent_observatory.services import auth as auth_svc

router = APIRouter(prefix="/api/auth", tags=["auth"])

SESSION_TTL = timedelta(days=30)


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=1, max_length=1000)


class PrincipalPayload(BaseModel):
    id: str
    username: str
    roles: list[str]
    bootstrap: bool = False


@router.post("/login", response_model=PrincipalPayload)
async def login(body: LoginRequest, request: Request, response: Response, db: DB) -> PrincipalPayload:
    user = await users.authenticate(db, body.username, body.password)
    if user is None:
        await audit.record(
            db,
            action="auth.login",
            actor_id=None,
            actor_type=audit.ACTOR_SYSTEM,
            resource_type="user",
            resource_id=body.username,
            request_id=request.state.request_id if hasattr(request.state, "request_id") else None,
            ip_address=_client_ip(request),
            user_agent=_user_agent(request),
            detail={"username": body.username},
            result=audit.RESULT_FAILURE,
            error_kind="invalid_credentials",
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        )

    raw_token = auth_svc.new_session_token()
    session = SessionModel(
        id=auth_svc.new_session_id(),
        user_id=user.id,
        token_hash=auth_svc.token_digest(raw_token),
        expires_at=datetime.now(UTC) + SESSION_TTL,
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
    )
    db.add(session)
    await audit.record(
        db,
        action="auth.login",
        actor_id=user.id,
        resource_type="user",
        resource_id=user.id,
        request_id=getattr(request.state, "request_id", None),
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
        detail={"username": user.username},
    )
    response.set_cookie(
        key=AUTH_COOKIE,
        value=raw_token,
        httponly=True,
        secure=False,  # local-first; HTTPS termination handled by reverse proxy in prod
        samesite="lax",
        max_age=int(SESSION_TTL.total_seconds()),
        path="/",
    )
    return PrincipalPayload(
        id=user.id,
        username=user.username,
        roles=sorted(user.role_names),
    )


class CreateTokenRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class CreatedTokenPayload(BaseModel):
    id: str
    name: str
    token: str
    expires_at: datetime | None


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=1000)
    new_password: str = Field(..., min_length=10, max_length=1000)


class TokenInfoPayload(BaseModel):
    id: str
    name: str
    created_at: datetime | None
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None


@router.post("/tokens", response_model=CreatedTokenPayload, status_code=201)
async def create_api_token(
    body: CreateTokenRequest,
    request: Request,
    db: DB,
    principal: CurrentPrincipal,
) -> CreatedTokenPayload:
    raw_token = auth_svc.new_session_token()
    expires_at = (
        datetime.now(UTC) + timedelta(days=body.expires_in_days)
        if body.expires_in_days is not None
        else None
    )
    token = ApiToken(
        id=auth_svc.new_session_id(),
        user_id=principal.user_id,
        name=body.name.strip(),
        token_hash=auth_svc.token_digest(raw_token),
        expires_at=expires_at,
    )
    db.add(token)
    await audit.record(
        db,
        action="auth.api_token_created",
        actor_id=principal.user_id,
        resource_type="api_token",
        resource_id=token.id,
        request_id=getattr(request.state, "request_id", None),
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
        detail={"name": token.name, "expires_at": expires_at.isoformat() if expires_at else None},
    )
    return CreatedTokenPayload(
        id=token.id,
        name=token.name,
        token=raw_token,
        expires_at=expires_at,
    )


@router.post("/password")
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    db: DB,
    principal: CurrentPrincipal,
) -> dict[str, bool]:
    user = await users.by_id(db, principal.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        await users.change_password(
            db,
            user,
            current_password=body.current_password,
            new_password=body.new_password,
        )
    except ValueError as exc:
        await audit.record(
            db,
            action="auth.password_changed",
            actor_id=principal.user_id,
            resource_type="user",
            resource_id=principal.user_id,
            request_id=getattr(request.state, "request_id", None),
            ip_address=_client_ip(request),
            user_agent=_user_agent(request),
            result=audit.RESULT_FAILURE,
            error_kind="validation",
            error_message=str(exc),
        )
        await db.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await audit.record(
        db,
        action="auth.password_changed",
        actor_id=principal.user_id,
        resource_type="user",
        resource_id=principal.user_id,
        request_id=getattr(request.state, "request_id", None),
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
    )
    return {"ok": True}


@router.get("/tokens", response_model=list[TokenInfoPayload])
async def list_api_tokens(db: DB, principal: CurrentPrincipal) -> list[TokenInfoPayload]:
    rows = (
        await db.scalars(
            select(ApiToken)
            .where(ApiToken.user_id == principal.user_id)
            .order_by(ApiToken.created_at.desc())
        )
    ).all()
    return [
        TokenInfoPayload(
            id=row.id,
            name=row.name,
            created_at=row.created_at,
            expires_at=row.expires_at,
            last_used_at=row.last_used_at,
            revoked_at=row.revoked_at,
        )
        for row in rows
    ]


@router.delete("/tokens/{token_id}")
async def revoke_api_token(
    token_id: str,
    request: Request,
    db: DB,
    principal: CurrentPrincipal,
) -> dict[str, bool]:
    row = await db.scalar(
        select(ApiToken).where(
            ApiToken.id == token_id,
            ApiToken.user_id == principal.user_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Token not found")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
    await audit.record(
        db,
        action="auth.api_token_revoked",
        actor_id=principal.user_id,
        resource_type="api_token",
        resource_id=row.id,
        request_id=getattr(request.state, "request_id", None),
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
        detail={"name": row.name},
    )
    return {"ok": True}


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    db: DB,
    principal: CurrentPrincipal,
) -> dict[str, bool]:
    from sqlalchemy import select

    digest = auth_svc.token_digest(
        _read_cookie(request, AUTH_COOKIE) or ""
    )
    row = await db.scalar(select(SessionModel).where(SessionModel.token_hash == digest))
    if row is not None and row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
    await audit.record(
        db,
        action="auth.logout",
        actor_id=principal.user_id,
        resource_type="user",
        resource_id=principal.user_id,
        request_id=getattr(request.state, "request_id", None),
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
    )
    response.delete_cookie(AUTH_COOKIE, path="/")
    return {"ok": True}


@router.get("/me", response_model=PrincipalPayload)
async def me(principal: CurrentPrincipal) -> PrincipalPayload:
    return PrincipalPayload(
        id=principal.user_id,
        username=principal.username,
        roles=sorted(principal.roles),
        bootstrap=principal.bootstrap,
    )


def _read_cookie(request: Request, name: str) -> str | None:
    return request.cookies.get(name)


__all__ = ["router"]
