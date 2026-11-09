"""Authenticated project-level token analytics API."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from agent_observatory.api.deps import DB, CurrentPrincipal
from agent_observatory.services import projects as service

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.get("")
async def list_projects(
    db: DB,
    principal: CurrentPrincipal,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    q: str | None = None,
    range_name: str = Query("month", alias="range"),
    agent: str | None = Query(None, min_length=1, max_length=32),
    model: str | None = None,
):
    try:
        return await service.list_projects(
            db,
            limit=limit,
            offset=offset,
            q=q,
            range_name=range_name,
            agent=agent,
            model=model,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{workspace_id}")
async def get_project(
    workspace_id: str,
    db: DB,
    principal: CurrentPrincipal,
):
    result = await service.get_project(db, workspace_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return result


@router.get("/{workspace_id}/timeseries")
async def get_project_timeseries(
    workspace_id: str,
    db: DB,
    principal: CurrentPrincipal,
    range_name: str = Query("day", alias="range"),
):
    try:
        result = await service.project_timeseries(
            db, workspace_id, range_name=range_name
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return result


__all__ = ["router"]
