"""Storage / retention API.

Read endpoints are open to operators, the destructive endpoint is admin-only
and never executes anything inline: it writes a ``system_commands`` row that
the local Supervisor applies (AGENTS.md: control actions go through the
command table first, and every one of them is audited).
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from agent_observatory.api.deps import (
    DB,
    AdminPrincipal,
    OperatorPrincipal,
    _client_ip,
    _user_agent,
)
from agent_observatory.config import settings
from agent_observatory.db.models import RetentionRun, SystemCommand
from agent_observatory.services import audit
from agent_observatory.services import retention as service
from agent_observatory.supervisor import supervisor_instance_id

router = APIRouter(prefix="/api/retention", tags=["retention"])


class SelectionPayload(BaseModel):
    keep_days: int = Field(default=7, ge=1, le=3650)
    agents: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    entry_ids: list[str] = Field(default_factory=list)
    include_raw: bool = True
    include_index: bool = True

    def to_selection(self) -> service.Selection:
        return service.Selection.from_payload(self.model_dump())


class PreviewRequest(BaseModel):
    selection: SelectionPayload = Field(default_factory=SelectionPayload)


class RunRequest(BaseModel):
    plan_token: str = Field(..., min_length=16, max_length=8192)
    confirm: bool = False
    acknowledge_large: bool = False


def _selection_payload(selection: service.Selection) -> dict[str, Any]:
    return selection.to_payload()


@router.get("/overview")
async def get_overview(
    db: DB,
    principal: OperatorPrincipal,
    refresh: bool = False,
) -> dict[str, Any]:
    try:
        payload = await service.overview(db, force=refresh)
    except OSError as exc:  # pragma: no cover - unexpected filesystem failure
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return payload


@router.get("/catalog")
async def get_catalog(principal: OperatorPrincipal) -> dict[str, Any]:
    config = settings()
    return {
        "enabled": bool(config.retention_enabled),
        "min_hours": int(config.retention_min_hours),
        "large_run_bytes": int(config.retention_large_run_bytes),
        "protected_roots": list(config.retention_protected_roots),
        "entries": [
            {
                "id": entry.id,
                "agent": entry.agent,
                "category": entry.category,
                "label": entry.label,
                "root": entry.root,
                "risk": entry.risk,
                "unit": entry.unit,
                "description": entry.description,
            }
            for entry in service.catalog(config=config)
        ],
    }


@router.post("/preview")
async def post_preview(
    body: PreviewRequest,
    db: DB,
    principal: OperatorPrincipal,
) -> dict[str, Any]:
    try:
        selection = body.selection.to_selection()
        planned = service.plan(selection)
        await service.enrich_plan(db, planned)
    except service.RetentionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    summary = planned.summary()
    summary["plan_token"] = service.sign_plan(selection, summary, actor_id=principal.user_id)
    summary["selection"] = _selection_payload(selection)
    summary["protected_roots"] = list(settings().retention_protected_roots)
    summary["large_run_bytes"] = int(settings().retention_large_run_bytes)
    summary["items"] = [
        {
            "path": item.path,
            "entry_id": item.entry_id,
            "agent": item.agent,
            "category": item.category,
            "risk": item.risk,
            "kind": item.kind,
            "bytes": item.bytes,
            "mtime": item.mtime,
        }
        for item in planned.items[:500]
    ]
    summary["items_truncated"] = len(planned.items) > 500
    return summary


@router.post("/runs", status_code=201)
async def create_run(
    body: RunRequest,
    request: Request,
    db: DB,
    principal: AdminPrincipal,
) -> dict[str, Any]:
    if not settings().retention_enabled:
        raise HTTPException(status_code=409, detail="retention is disabled")
    if not body.confirm:
        raise HTTPException(status_code=400, detail="confirm must be true")
    try:
        sealed = service.verify_plan(body.plan_token)
    except service.RetentionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if sealed.get("actor") not in (None, "", principal.user_id):
        raise HTTPException(status_code=403, detail="plan was issued for another user")
    try:
        selection = service.Selection.from_payload(sealed.get("selection"))
        planned = service.plan(selection)
        await service.enrich_plan(db, planned)
    except service.RetentionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    summary = planned.summary()
    previewed_bytes = int(sealed.get("bytes") or 0)
    if (
        planned.bytes_total
        > previewed_bytes * 1.10 + int(settings().retention_plan_drift_bytes)
    ):
        raise HTTPException(
            status_code=409,
            detail="selected data changed since the preview; preview again",
        )
    if (
        planned.bytes_total > int(settings().retention_large_run_bytes)
        and not body.acknowledge_large
    ):
        raise HTTPException(
            status_code=400,
            detail="this run is larger than the configured threshold; acknowledge it first",
        )
    if not planned.items and not (selection.include_index and planned.index.get("sessions")):
        raise HTTPException(status_code=400, detail="nothing to delete for this selection")

    scope = {
        (True, True): "raw+index",
        (True, False): "raw",
        (False, True): "index",
        (False, False): "none",
    }[(selection.include_raw, selection.include_index)]

    run = await service.create_run(
        db,
        selection=selection,
        plan_obj=planned,
        principal_id=principal.user_id,
        scope=scope,
    )
    command = SystemCommand(
        command_type="retention.purge",
        component=None,
        target_instance=supervisor_instance_id(settings().home),
        requested_by=principal.user_id,
        payload={"run_id": run.id},
    )
    db.add(command)
    await db.flush()
    run.command_id = int(command.id)
    await audit.record(
        db,
        action="retention.purge.requested",
        actor_id=principal.user_id,
        resource_type="retention_run",
        resource_id=run.id,
        request_id=getattr(request.state, "request_id", None),
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
        detail={
            "command_id": int(command.id),
            "scope": scope,
            "keep_days": selection.keep_days,
            "selection": _selection_payload(selection),
            "planned_bytes": planned.bytes_total,
            "planned_files": len(planned.items),
        },
    )
    await db.flush()
    _wake_supervisor()
    return {
        "run": service.serialize_run(run),
        "command_id": int(command.id),
    }


def _wake_supervisor() -> None:
    """Touch the supervisor wakeup file so a purge starts immediately."""
    try:
        wakeup = Path(settings().home).expanduser() / "run" / "supervisor.wakeup"
        wakeup.parent.mkdir(parents=True, exist_ok=True)
        wakeup.touch()
    except OSError:  # pragma: no cover - the supervisor still polls
        pass


@router.get("/runs")
async def list_runs(
    db: DB,
    principal: OperatorPrincipal,
    limit: int = 20,
) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
    rows = await service.list_runs(db, limit=limit)
    return {"runs": [service.serialize_run(row) for row in rows]}


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    db: DB,
    principal: OperatorPrincipal,
) -> dict[str, Any]:
    row = await db.get(RetentionRun, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="retention run not found")
    payload = service.serialize_run(row)
    payload["generated_at"] = datetime.now(UTC).isoformat()
    return payload


__all__ = ["router"]
