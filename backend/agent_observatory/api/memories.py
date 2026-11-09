from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from agent_observatory.api.deps import (
    DB,
    CurrentPrincipal,
    OperatorPrincipal,
    _client_ip,
    _user_agent,
)
from agent_observatory.db.models import MemoryEntry
from agent_observatory.services import audit, memories

router = APIRouter(prefix="/api/memories", tags=["memories"])


class ContentRequest(BaseModel):
    format: Literal["raw", "markdown"] = "raw"
    purpose: Literal["reveal", "copy"] = "copy"


class ExportRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=200)
    format: Literal["markdown", "zip"] = "zip"
    target: Literal["generic", "codex", "stepcode", "claude"] = "generic"


async def _entry(db: DB, entry_id: str) -> MemoryEntry:
    row = await memories.get_entry(db, entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Memory entry not found")
    return row


async def _record_action(
    db: DB,
    request: Request,
    principal,
    *,
    action: str,
    resource_id: str,
    detail: dict,
) -> None:
    await audit.record(
        db,
        action=action,
        actor_id=principal.user_id,
        resource_type="memory_entry",
        resource_id=resource_id,
        request_id=getattr(request.state, "request_id", None),
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
        detail=detail,
    )


@router.get("")
async def list_memories(
    db: DB,
    principal: CurrentPrincipal,
    limit: int = Query(100, ge=1, le=200),
    cursor: str | None = None,
    q: str | None = None,
    agent: str | None = Query(None, min_length=1, max_length=32),
    scope: str | None = Query(None, pattern="^(global|project|thread)$"),
    workspace_id: str | None = None,
    include_missing: bool = False,
):
    del principal
    try:
        return await memories.list_entries(
            db,
            limit=limit,
            cursor=cursor,
            query=q,
            agent=agent,
            scope=scope,
            workspace_id=workspace_id,
            include_missing=include_missing,
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="invalid cursor") from exc


@router.get("/sources")
async def memory_sources(db: DB, principal: CurrentPrincipal):
    del principal
    return await memories.source_status(db)


@router.get("/{entry_id}")
async def memory_detail(
    entry_id: str,
    request: Request,
    db: DB,
    principal: CurrentPrincipal,
):
    row = await _entry(db, entry_id)
    workspace = await memories.get_workspace(db, row.workspace_id)
    await _record_action(
        db,
        request,
        principal,
        action="memory.viewed",
        resource_id=row.id,
        detail={
            "agent": row.agent,
            "sensitivity": row.sensitivity,
            "content_redacted": row.sensitivity == "sensitive",
        },
    )
    return memories.serialize_detail(row, workspace, reveal=False)


@router.post("/{entry_id}/content")
async def memory_content(
    entry_id: str,
    payload: ContentRequest,
    request: Request,
    db: DB,
    principal: OperatorPrincipal,
):
    row = await _entry(db, entry_id)
    content = (
        memories.render_markdown([row], target=row.agent)
        if payload.format == "markdown"
        else row.content
    )
    await _record_action(
        db,
        request,
        principal,
        action=(
            "memory.content_revealed"
            if payload.purpose == "reveal"
            else "memory.copied"
        ),
        resource_id=row.id,
        detail={
            "agent": row.agent,
            "format": payload.format,
            "content_hash": row.content_hash,
            "sensitivity": row.sensitivity,
        },
    )
    return {
        "id": row.id,
        "format": payload.format,
        "content": content,
        "content_hash": row.content_hash,
    }


@router.post("/export")
async def export_memories(
    payload: ExportRequest,
    request: Request,
    db: DB,
    principal: OperatorPrincipal,
):
    rows = list(
        (
            await db.scalars(
                select(MemoryEntry).where(MemoryEntry.id.in_(payload.ids))
            )
        ).all()
    )
    by_id = {row.id: row for row in rows}
    ordered = [by_id[entry_id] for entry_id in payload.ids if entry_id in by_id]
    if len(ordered) != len(payload.ids):
        raise HTTPException(status_code=404, detail="One or more memory entries were not found")
    try:
        result = memories.build_export(
            ordered,
            format_name=payload.format,
            target=payload.target,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await audit.record(
        db,
        action="memory.exported",
        actor_id=principal.user_id,
        resource_type="memory_entries",
        resource_id=",".join(payload.ids),
        request_id=getattr(request.state, "request_id", None),
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
        detail={
            "entry_count": len(ordered),
            "format": payload.format,
            "target": payload.target,
            "content_hashes": [row.content_hash for row in ordered],
        },
    )
    return result


__all__ = ["router"]
