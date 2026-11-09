"""Admin-only endpoints (Phase 1 subset).

The smallest protected surface that exercises `require_role(admin)` end to end:
list all users (never `password_hash`), create a user. Grows in later phases.
"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from agent_observatory.api.deps import DB, AdminPrincipal, _client_ip, _user_agent
from agent_observatory.db.models import ApiToken
from agent_observatory.services import audit, users

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/users")
async def list_users(
    request: Request,
    db: DB,
    principal: AdminPrincipal,
) -> dict:
    rows = await users.list_users(db)
    return {"users": [users.to_public_dict(u) for u in rows]}


class CreateUserRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=10, max_length=1000)
    roles: list[str] = ["viewer"]


class UpdateUserRequest(BaseModel):
    roles: list[str] | None = None
    is_active: bool | None = None


@router.post("/users", status_code=201)
async def create_user(
    body: CreateUserRequest,
    request: Request,
    db: DB,
    principal: AdminPrincipal,
) -> dict:
    try:
        user = await users.create_user(
            db,
            username=body.username,
            password=body.password,
            roles=body.roles,
            actor_id=principal.user_id,
            actor_type=audit.ACTOR_USER,
        )
    except ValueError as exc:
        await audit.record(
            db,
            action="user.create",
            actor_id=principal.user_id,
            resource_type="user",
            request_id=getattr(request.state, "request_id", None),
            ip_address=_client_ip(request),
            user_agent=_user_agent(request),
            detail={"username": body.username, "roles": body.roles},
            result=audit.RESULT_FAILURE,
            error_kind="validation",
            error_message=str(exc),
        )
        await db.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return users.to_public_dict(user)


@router.patch("/users/{user_id}")
async def update_user(
    user_id: str,
    body: UpdateUserRequest,
    request: Request,
    db: DB,
    principal: AdminPrincipal,
) -> dict:
    user = await users.by_id(db, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if body.roles is None and body.is_active is None:
        raise HTTPException(status_code=400, detail="No changes supplied")
    if user.id == principal.user_id and body.is_active is False:
        await audit.record(
            db,
            action="user.updated",
            actor_id=principal.user_id,
            resource_type="user",
            resource_id=user.id,
            request_id=getattr(request.state, "request_id", None),
            ip_address=_client_ip(request),
            user_agent=_user_agent(request),
            detail={"requested_is_active": False},
            result=audit.RESULT_FAILURE,
            error_kind="validation",
            error_message="You cannot deactivate your own account",
        )
        await db.commit()
        raise HTTPException(status_code=400, detail="You cannot deactivate your own account")
    before = users.to_public_dict(user)
    try:
        if body.roles is not None:
            await users.set_roles(db, user, body.roles)
        if body.is_active is not None:
            await users.set_active(db, user, is_active=body.is_active)
    except ValueError as exc:
        await audit.record(
            db,
            action="user.updated",
            actor_id=principal.user_id,
            resource_type="user",
            resource_id=user.id,
            request_id=getattr(request.state, "request_id", None),
            ip_address=_client_ip(request),
            user_agent=_user_agent(request),
            detail={"before": before},
            result=audit.RESULT_FAILURE,
            error_kind="validation",
            error_message=str(exc),
        )
        await db.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.refresh(user)
    after = users.to_public_dict(user)
    await audit.record(
        db,
        action="user.updated",
        actor_id=principal.user_id,
        resource_type="user",
        resource_id=user.id,
        request_id=getattr(request.state, "request_id", None),
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
        detail={"before": before, "after": after},
    )
    return after


@router.delete("/users/{user_id}/tokens/{token_id}")
async def revoke_user_token(
    user_id: str,
    token_id: str,
    request: Request,
    db: DB,
    principal: AdminPrincipal,
) -> dict[str, bool]:
    row = await db.scalar(
        select(ApiToken).where(ApiToken.id == token_id, ApiToken.user_id == user_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Token not found")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
    await audit.record(
        db,
        action="admin.api_token_revoked",
        actor_id=principal.user_id,
        resource_type="api_token",
        resource_id=row.id,
        request_id=getattr(request.state, "request_id", None),
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
        detail={"user_id": user_id, "name": row.name},
    )
    return {"ok": True}


__all__ = ["router"]
