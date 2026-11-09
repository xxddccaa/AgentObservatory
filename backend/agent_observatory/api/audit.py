from __future__ import annotations

import csv
import io
import json

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from agent_observatory.api.deps import DB, CurrentPrincipal
from agent_observatory.db.models import AuditLog
from agent_observatory.services.audit import list_audit

router = APIRouter(prefix="/api/audit", tags=["audit"])


def _serialize(row: AuditLog) -> dict:
    return {
        "id": row.id,
        "actor_id": row.actor_id,
        "actor_type": row.actor_type,
        "action": row.action,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "request_id": row.request_id,
        "session_id": row.session_id,
        "detail": row.detail,
        "result": row.result,
        "error_kind": row.error_kind,
        "error_message": row.error_message,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("")
async def audit_list(
    db: DB,
    principal: CurrentPrincipal,
    limit: int = Query(default=100, ge=1, le=500),
    cursor: int | None = Query(default=None),
    actor_id: str | None = None,
    action: str | None = None,
):
    rows = await list_audit(
        db, limit=limit, cursor=cursor, actor_id=actor_id, action=action
    )
    return {"items": [_serialize(row) for row in rows], "next_cursor": rows[-1].id if rows else None}


@router.get("/export")
async def audit_export(
    db: DB,
    principal: CurrentPrincipal,
    format: str = Query(default="json", pattern="^(json|csv)$"),
    limit: int = Query(default=1000, ge=1, le=10000),
):
    rows = await list_audit(db, limit=limit)
    items = [_serialize(row) for row in rows]
    if format == "json":
        content = json.dumps(items, ensure_ascii=False, indent=2).encode()
        media_type = "application/json"
        filename = "agent-observatory-audit.json"
    else:
        buffer = io.StringIO()
        writer = csv.DictWriter(
            buffer,
            fieldnames=["id", "actor_id", "action", "resource_type", "resource_id", "result", "created_at"],
        )
        writer.writeheader()
        for item in items:
            writer.writerow({key: item.get(key) for key in writer.fieldnames})
        content = buffer.getvalue().encode()
        media_type = "text/csv"
        filename = "agent-observatory-audit.csv"
    return StreamingResponse(
        iter([content]),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
