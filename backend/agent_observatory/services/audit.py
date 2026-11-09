"""Audit logging.

Append-only writes to `audit_logs`. Every auth event and every later control
action flows through `record(...)`. Secrets never land here: callers pass a
redacted `detail` dict (see AGENTS.md #19). The raw request id / ip / ua are
threaded in from the request context so a single call site can emit a fully
attributed row.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_observatory.db.models import AuditLog

# actor_type ∈ {user, system, bootstrap}; result ∈ {success, failure, error}
ACTOR_USER = "user"
ACTOR_SYSTEM = "system"
ACTOR_BOOTSTRAP = "bootstrap"

RESULT_SUCCESS = "success"
RESULT_FAILURE = "failure"
RESULT_ERROR = "error"


async def record(
    session: AsyncSession,
    *,
    action: str,
    actor_id: str | None = None,
    actor_type: str = ACTOR_USER,
    resource_type: str | None = None,
    resource_id: str | None = None,
    request_id: str | None = None,
    session_id: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    detail: dict[str, Any] | None = None,
    result: str = RESULT_SUCCESS,
    error_kind: str | None = None,
    error_message: str | None = None,
) -> AuditLog:
    row = AuditLog(
        actor_id=actor_id,
        actor_type=actor_type,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        request_id=request_id,
        session_id=session_id,
        ip_address=ip_address,
        user_agent=user_agent,
        detail=detail or {},
        result=result,
        error_kind=error_kind,
        error_message=(error_message[:500] if error_message else None),
    )
    session.add(row)
    await session.flush()
    return row


async def list_audit(
    session: AsyncSession,
    *,
    limit: int = 100,
    cursor: int | None = None,
    actor_id: str | None = None,
    action: str | None = None,
) -> list[AuditLog]:
    stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
    if cursor is not None:
        stmt = stmt.where(AuditLog.id < cursor)
    if actor_id:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    rows = (await session.scalars(stmt)).all()
    return list(rows)


__all__ = [
    "record",
    "list_audit",
    "ACTOR_USER",
    "ACTOR_SYSTEM",
    "ACTOR_BOOTSTRAP",
    "RESULT_SUCCESS",
    "RESULT_FAILURE",
    "RESULT_ERROR",
]
