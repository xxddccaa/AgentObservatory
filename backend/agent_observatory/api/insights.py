"""Authenticated read-only Insights API."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request

from agent_observatory.api.deps import (
    DB,
    CurrentPrincipal,
    _client_ip,
    _user_agent,
)
from agent_observatory.services import audit
from agent_observatory.services import insights as service

router = APIRouter(prefix="/api/insights", tags=["insights"])


def _parse_datetime(value: str | None, label: str) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid {label} timestamp") from exc


def _filters(
    from_value: str | None,
    to_value: str | None,
    workspace_id: str | None,
    model: str | None,
    agent: str | None,
) -> service.InsightFilters:
    try:
        return service.resolve_range(
            _parse_datetime(from_value, "from"),
            _parse_datetime(to_value, "to"),
            workspace_id=workspace_id,
            model=model,
            agent=agent,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/overview")
async def get_overview(
    db: DB,
    principal: CurrentPrincipal,
    from_value: str | None = Query(None, alias="from"),
    to_value: str | None = Query(None, alias="to"),
    workspace_id: str | None = None,
    model: str | None = None,
    agent: str | None = Query(None, min_length=1, max_length=32),
):
    return await service.overview(
        db, _filters(from_value, to_value, workspace_id, model, agent)
    )


@router.get("/timeseries")
async def get_timeseries(
    db: DB,
    principal: CurrentPrincipal,
    from_value: str | None = Query(None, alias="from"),
    to_value: str | None = Query(None, alias="to"),
    workspace_id: str | None = None,
    model: str | None = None,
    agent: str | None = Query(None, min_length=1, max_length=32),
):
    return await service.timeseries(
        db, _filters(from_value, to_value, workspace_id, model, agent)
    )


@router.get("/latency")
async def get_latency(
    db: DB,
    principal: CurrentPrincipal,
    from_value: str | None = Query(None, alias="from"),
    to_value: str | None = Query(None, alias="to"),
    workspace_id: str | None = None,
    model: str | None = None,
    agent: str | None = Query(None, min_length=1, max_length=32),
):
    return await service.latency(
        db, _filters(from_value, to_value, workspace_id, model, agent)
    )


@router.get("/tools")
async def get_tools(
    db: DB,
    principal: CurrentPrincipal,
    from_value: str | None = Query(None, alias="from"),
    to_value: str | None = Query(None, alias="to"),
    workspace_id: str | None = None,
    model: str | None = None,
    agent: str | None = Query(None, min_length=1, max_length=32),
    tool: str | None = None,
    status: str | None = None,
):
    return await service.tools(
        db,
        _filters(from_value, to_value, workspace_id, model, agent),
        tool_name=tool,
        status=status,
    )


@router.get("/skills")
async def get_skills(
    db: DB,
    principal: CurrentPrincipal,
    from_value: str | None = Query(None, alias="from"),
    to_value: str | None = Query(None, alias="to"),
    workspace_id: str | None = None,
    model: str | None = None,
    agent: str | None = Query(None, min_length=1, max_length=32),
    confidence: str | None = None,
):
    return await service.skills(
        db,
        _filters(from_value, to_value, workspace_id, model, agent),
        confidence=confidence,
    )


@router.get("/agents")
async def get_agents(
    db: DB,
    principal: CurrentPrincipal,
    from_value: str | None = Query(None, alias="from"),
    to_value: str | None = Query(None, alias="to"),
    workspace_id: str | None = None,
    model: str | None = None,
    agent: str | None = Query(None, min_length=1, max_length=32),
):
    return await service.agents(
        db, _filters(from_value, to_value, workspace_id, model, agent)
    )


@router.get("/sources")
async def get_sources(
    db: DB,
    principal: CurrentPrincipal,
    agent: str | None = Query(None, min_length=1, max_length=32),
    source: str | None = None,
    status: str | None = None,
):
    return await service.sources(
        db, agent=agent, source_kind=source, status=status
    )


@router.get("/sessions/{session_id}/timeline")
async def get_session_timeline(
    session_id: str,
    db: DB,
    principal: CurrentPrincipal,
    limit: int = Query(500, ge=1, le=2000),
):
    result = await service.session_timeline(db, session_id, limit=limit)
    if result is None:
        raise HTTPException(status_code=404, detail="Agent session not found")
    return result


@router.get("/evidence")
async def list_evidence(
    db: DB,
    principal: CurrentPrincipal,
    from_value: str | None = Query(None, alias="from"),
    to_value: str | None = Query(None, alias="to"),
    source_id: str | None = None,
    category: str | None = None,
    level: str | None = None,
    thread_id: str | None = None,
    limit: int = Query(100, ge=1, le=200),
):
    return await service.evidence_list(
        db,
        _filters(from_value, to_value, None, None, None),
        source_id=source_id,
        category=category,
        level=level,
        thread_id=thread_id,
        limit=limit,
    )


@router.get("/evidence/{evidence_id}")
async def get_evidence(
    evidence_id: str,
    request: Request,
    db: DB,
    principal: CurrentPrincipal,
):
    result = await service.evidence(db, evidence_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Evidence not found")
    await audit.record(
        db,
        action="insights.evidence.viewed",
        actor_id=principal.user_id,
        resource_type="evidence_record",
        resource_id=evidence_id,
        request_id=getattr(request.state, "request_id", None),
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
        detail={"category": result["category"], "source_id": result["source_id"]},
    )
    return result


@router.get("/export")
async def export_insights(
    request: Request,
    db: DB,
    principal: CurrentPrincipal,
    format_name: str = Query("json", alias="format"),
    from_value: str | None = Query(None, alias="from"),
    to_value: str | None = Query(None, alias="to"),
    workspace_id: str | None = None,
    model: str | None = None,
    agent: str | None = Query(None, min_length=1, max_length=32),
):
    if format_name not in {"csv", "json"}:
        raise HTTPException(status_code=400, detail="format must be csv or json")
    filters = _filters(from_value, to_value, workspace_id, model, agent)
    result = await service.export_data(db, filters, format_name=format_name)
    await audit.record(
        db,
        action="insights.exported",
        actor_id=principal.user_id,
        resource_type="insights",
        resource_id=format_name,
        request_id=getattr(request.state, "request_id", None),
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
        detail={
            "format": format_name,
            "from": filters.since.isoformat(),
            "to": filters.until.isoformat(),
            "workspace_id": workspace_id,
            "model": model,
            "agent": agent,
        },
    )
    return result


__all__ = ["router"]
