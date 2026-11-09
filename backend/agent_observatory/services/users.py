"""User management: provisioning, lookup, role seeding.

`seed_roles` is idempotent and called at startup and at the start of any test
that needs RBAC. `create_user` provisions a user with a hashed password and a
set of role names, refusing duplicate usernames. Passwords are never returned
to callers — `to_public_dict` strips `password_hash`.
"""
from __future__ import annotations

import secrets
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_observatory.db.models import ApiToken, Role, User
from agent_observatory.db.models import Session as SessionModel
from agent_observatory.services import auth
from agent_observatory.services.audit import ACTOR_SYSTEM, record

KNOWN_ROLES = ("admin", "operator", "viewer")


def new_id() -> str:
    return secrets.token_hex(8)


async def seed_roles(session: AsyncSession) -> dict[str, int]:
    """Ensure every known role exists; return {name: id}.

    Idempotent. Must be awaited before any user with roles is created.
    """
    existing = {
        r.name: r
        for r in (await session.scalars(select(Role).where(Role.name.in_(KNOWN_ROLES)))).all()
    }
    result: dict[str, int] = {}
    for name in KNOWN_ROLES:
        role = existing.get(name)
        if role is None:
            role = Role(name=name)
            session.add(role)
            await session.flush()
        result[name] = role.id
    return result


async def by_username(session: AsyncSession, username: str) -> User | None:
    stmt = select(User).where(User.username == username)
    return await session.scalar(stmt)


async def by_id(session: AsyncSession, user_id: str) -> User | None:
    return await session.get(User, user_id)


async def create_user(
    session: AsyncSession,
    *,
    username: str,
    password: str,
    roles: list[str],
    actor_id: str | None = None,
    actor_type: str = ACTOR_SYSTEM,
) -> User:
    username = (username or "").strip()
    if not username:
        raise ValueError("username is required")
    if await by_username(session, username) is not None:
        raise ValueError(f"username already exists: {username}")
    unknown = set(roles) - set(KNOWN_ROLES)
    if unknown:
        raise ValueError(f"unknown roles: {sorted(unknown)}")

    await seed_roles(session)
    role_rows = {
        role.name: role
        for role in (
            await session.scalars(select(Role).where(Role.name.in_(roles)))
        ).all()
    }
    user = User(
        id=new_id(),
        username=username,
        password_hash=auth.hash_password(password),
        is_active=True,
    )
    user.roles.extend(role_rows[name] for name in roles)
    session.add(user)
    await session.flush()
    await record(
        session,
        action="user.created",
        actor_id=actor_id,
        actor_type=actor_type,
        resource_type="user",
        resource_id=user.id,
        detail={"username": user.username, "roles": sorted(roles)},
    )
    return user


async def authenticate(session: AsyncSession, username: str, password: str) -> User | None:
    """Return the active user if credentials match, else None."""
    user = await by_username(session, (username or "").strip())
    if user is None or not user.is_active:
        return None
    if not auth.verify_password(password, user.password_hash):
        return None
    return user


async def list_users(session: AsyncSession) -> list[User]:
    rows = await session.scalars(select(User).order_by(User.username))
    return list(rows)


async def change_password(
    session: AsyncSession,
    user: User,
    *,
    current_password: str,
    new_password: str,
) -> None:
    """Replace a user's password after verifying the existing password."""
    if not auth.verify_password(current_password, user.password_hash):
        raise ValueError("Current password is incorrect")
    user.password_hash = auth.hash_password(new_password)
    await session.flush()


async def revoke_all_credentials(session: AsyncSession, user_id: str) -> tuple[int, int]:
    """Revoke every active session and API token for a user.

    Returns ``(revoked_sessions, revoked_tokens)``. Already-revoked rows are
    left untouched. Callers own the transaction and must commit.
    """
    now = datetime.now(UTC)
    sessions = (
        await session.scalars(
            select(SessionModel).where(
                SessionModel.user_id == user_id,
                SessionModel.revoked_at.is_(None),
            )
        )
    ).all()
    for row in sessions:
        row.revoked_at = now
    tokens = (
        await session.scalars(
            select(ApiToken).where(
                ApiToken.user_id == user_id,
                ApiToken.revoked_at.is_(None),
            )
        )
    ).all()
    for row in tokens:
        row.revoked_at = now
    await session.flush()
    return len(sessions), len(tokens)


async def set_active(session: AsyncSession, user: User, *, is_active: bool) -> None:
    """Enable or disable a user and invalidate active credentials on disable."""
    user.is_active = is_active
    if not is_active:
        await revoke_all_credentials(session, user.id)
    await session.flush()


async def set_roles(session: AsyncSession, user: User, roles: list[str]) -> None:
    """Replace roles, rejecting unknown names and empty role sets."""
    normalized = sorted({name.strip() for name in roles if name and name.strip()})
    if not normalized:
        raise ValueError("at least one role is required")
    unknown = set(normalized) - set(KNOWN_ROLES)
    if unknown:
        raise ValueError(f"unknown roles: {sorted(unknown)}")
    await seed_roles(session)
    role_rows = {
        role.name: role
        for role in (
            await session.scalars(select(Role).where(Role.name.in_(normalized)))
        ).all()
    }
    user.roles = [role_rows[name] for name in normalized]
    await session.flush()


def to_public_dict(user: User) -> dict:
    """Serialize a user without ever exposing `password_hash` (AGENTS.md #19)."""
    return {
        "id": user.id,
        "username": user.username,
        "is_active": user.is_active,
        "roles": sorted(user.role_names),
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "updated_at": user.updated_at.isoformat() if user.updated_at else None,
    }


__all__ = [
    "seed_roles",
    "by_username",
    "by_id",
    "create_user",
    "authenticate",
    "list_users",
    "change_password",
    "set_active",
    "set_roles",
    "to_public_dict",
    "KNOWN_ROLES",
    "new_id",
]
