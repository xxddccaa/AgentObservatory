"""PostgreSQL-backed analytics for the local agent evidence index."""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from agent_observatory.db.models import (
    ActivitySpan,
    AgentEvent,
    AgentSession,
    AgentTurn,
    EvidenceRecord,
    LocalDataSource,
    SessionSkillEvidence,
    SkillDefinition,
    StepGoRun,
    ThreadEdge,
    ThreadGoal,
    ToolCall,
)
from agent_observatory.services.redaction import redact_secrets, redact_value

DEFAULT_RANGE = timedelta(days=30)
MAX_TIMESERIES_POINTS = 500
MAX_AGENT_EDGES = 2_000
CONFIDENCE_LEVELS = ("exact", "derived", "inferred")


@dataclass(frozen=True)
class InsightFilters:
    since: datetime
    until: datetime
    workspace_id: str | None = None
    model: str | None = None
    agent: str | None = None


def utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must include a UTC offset")
    return value.astimezone(UTC)


def resolve_range(
    since: datetime | None,
    until: datetime | None,
    *,
    now: datetime | None = None,
    workspace_id: str | None = None,
    model: str | None = None,
    agent: str | None = None,
) -> InsightFilters:
    resolved_until = utc_datetime(until) if until else utc_datetime(now or datetime.now(UTC))
    resolved_since = utc_datetime(since) if since else resolved_until - DEFAULT_RANGE
    if resolved_since >= resolved_until:
        raise ValueError("from must be earlier than to")
    return InsightFilters(
        since=resolved_since,
        until=resolved_until,
        workspace_id=workspace_id,
        model=model,
        agent=agent,
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _number(value: Any, *, integer: bool = False) -> int | float:
    if value is None:
        return 0
    return int(value) if integer else float(value)


def _turn_activity_time():
    return func.coalesce(
        AgentTurn.completed_at,
        AgentTurn.started_at,
        AgentTurn.created_at,
    )


def _tool_activity_time():
    return func.coalesce(
        ToolCall.started_at,
        ToolCall.completed_at,
        ToolCall.created_at,
    )


def _session_dimensions(filters: InsightFilters) -> list[Any]:
    clauses: list[Any] = []
    if filters.workspace_id:
        clauses.append(AgentSession.workspace_id == filters.workspace_id)
    if filters.model:
        clauses.append(AgentSession.model == filters.model)
    if filters.agent == "stepgo":
        clauses.append(
            exists().where(
                StepGoRun.linked_agent_session_id == AgentSession.id
            )
        )
    elif filters.agent:
        clauses.append(AgentSession.agent == filters.agent)
    return clauses


def _turn_where(filters: InsightFilters) -> list[Any]:
    return [
        _turn_activity_time() >= filters.since,
        _turn_activity_time() < filters.until,
        *_session_dimensions(filters),
    ]


def _tool_where(filters: InsightFilters) -> list[Any]:
    return [
        _tool_activity_time() >= filters.since,
        _tool_activity_time() < filters.until,
        *_session_dimensions(filters),
    ]


async def _group_counts(
    db: AsyncSession,
    column: Any,
    filters: InsightFilters,
) -> dict[str, int]:
    rows = (
        await db.execute(
            select(column, func.count(AgentTurn.id))
            .select_from(AgentTurn)
            .join(AgentSession, AgentSession.id == AgentTurn.agent_session_id)
            .where(*_turn_where(filters))
            .group_by(column)
        )
    ).all()
    return {str(key or "unknown"): int(count) for key, count in rows}


def _confidence_dict(counts: dict[str, int]) -> dict[str, int]:
    return {
        **{level: int(counts.get(level, 0)) for level in CONFIDENCE_LEVELS},
        "unknown": sum(
            count for level, count in counts.items() if level not in CONFIDENCE_LEVELS
        ),
    }


async def overview(db: AsyncSession, filters: InsightFilters) -> dict[str, Any]:
    session_row = (
        await db.execute(
            select(func.count(func.distinct(AgentTurn.agent_session_id)))
            .select_from(AgentTurn)
            .join(AgentSession, AgentSession.id == AgentTurn.agent_session_id)
            .where(*_turn_where(filters))
        )
    ).one()
    turn_row = (
        await db.execute(
            select(
                func.count(AgentTurn.id),
                func.coalesce(func.sum(AgentTurn.total_tokens), 0),
                func.coalesce(func.sum(AgentTurn.input_tokens), 0),
                func.coalesce(func.sum(AgentTurn.cached_input_tokens), 0),
                func.coalesce(func.sum(AgentTurn.output_tokens), 0),
                func.coalesce(func.sum(AgentTurn.reasoning_output_tokens), 0),
            )
            .select_from(AgentTurn)
            .join(AgentSession, AgentSession.id == AgentTurn.agent_session_id)
            .where(*_turn_where(filters))
        )
    ).one()
    outcomes = await _group_counts(db, AgentTurn.outcome, filters)
    validations = await _group_counts(db, AgentTurn.validation_status, filters)
    confidence = _confidence_dict(
        await _group_counts(db, AgentTurn.metric_confidence, filters)
    )
    tool_failed = or_(
        ToolCall.status.in_(("failed", "error")),
        and_(ToolCall.exit_code.is_not(None), ToolCall.exit_code != 0),
    )
    by_agent_rows = (
        await db.execute(
            select(
                AgentTurn.agent,
                func.count(func.distinct(AgentTurn.agent_session_id)).label(
                    "session_count"
                ),
                func.count(AgentTurn.id).label("turn_count"),
                func.coalesce(func.sum(AgentTurn.total_tokens), 0).label(
                    "total_tokens"
                ),
            )
            .select_from(AgentTurn)
            .join(AgentSession, AgentSession.id == AgentTurn.agent_session_id)
            .where(*_turn_where(filters))
            .group_by(AgentTurn.agent)
            .order_by(AgentTurn.agent)
        )
    ).all()
    tool_by_agent_rows = (
        await db.execute(
            select(
                ToolCall.agent,
                func.count(ToolCall.id).label("tool_call_count"),
                func.count(ToolCall.id).filter(tool_failed).label("failure_count"),
            )
            .select_from(ToolCall)
            .join(AgentSession, AgentSession.id == ToolCall.agent_session_id)
            .where(*_tool_where(filters))
            .group_by(ToolCall.agent)
        )
    ).all()
    tool_by_agent = {
        row.agent: (int(row.tool_call_count), int(row.failure_count))
        for row in tool_by_agent_rows
    }
    return {
        "since": filters.since.isoformat(),
        "until": filters.until.isoformat(),
        "totals": {
            "session_count": int(session_row[0] or 0),
            "turn_count": int(turn_row[0] or 0),
            "total_tokens": int(turn_row[1] or 0),
            "input_tokens": int(turn_row[2] or 0),
            "cached_input_tokens": int(turn_row[3] or 0),
            "output_tokens": int(turn_row[4] or 0),
            "reasoning_output_tokens": int(turn_row[5] or 0),
        },
        "quality": {
            "outcomes": outcomes,
            "validation": validations,
            "confidence": confidence,
        },
        "by_agent": [
            {
                "agent": row.agent,
                "session_count": int(row.session_count or 0),
                "turn_count": int(row.turn_count or 0),
                "total_tokens": int(row.total_tokens or 0),
                "tool_call_count": tool_by_agent.get(row.agent, (0, 0))[0],
                "failure_count": tool_by_agent.get(row.agent, (0, 0))[1],
            }
            for row in by_agent_rows
        ],
    }


def _timeseries_bucket(filters: InsightFilters) -> str:
    duration = filters.until - filters.since
    if duration <= timedelta(hours=MAX_TIMESERIES_POINTS):
        return "hour"
    if duration <= timedelta(days=MAX_TIMESERIES_POINTS):
        return "day"
    if duration <= timedelta(weeks=MAX_TIMESERIES_POINTS):
        return "week"
    if duration <= timedelta(days=31 * MAX_TIMESERIES_POINTS):
        return "month"
    return "year"


async def timeseries(db: AsyncSession, filters: InsightFilters) -> dict[str, Any]:
    bucket_name = _timeseries_bucket(filters)
    activity_time = _turn_activity_time()
    bucket = func.date_trunc(bucket_name, activity_time, "UTC").label("bucket")
    successful = AgentTurn.outcome.in_(("completed", "success", "ok"))
    failed = AgentTurn.outcome.in_(("failed", "error", "interrupted"))
    rows = (
        await db.execute(
            select(
                bucket,
                func.count(AgentTurn.id).label("turn_count"),
                func.count(func.distinct(AgentTurn.agent_session_id)).label(
                    "session_count"
                ),
                func.coalesce(func.sum(AgentTurn.total_tokens), 0).label(
                    "total_tokens"
                ),
                func.count(AgentTurn.id)
                .filter(successful)
                .label("success_count"),
                func.count(AgentTurn.id)
                .filter(failed)
                .label("failure_count"),
                func.count(AgentTurn.id)
                .filter(~successful, ~failed)
                .label("unknown_count"),
            )
            .select_from(AgentTurn)
            .join(AgentSession, AgentSession.id == AgentTurn.agent_session_id)
            .where(*_turn_where(filters))
            .group_by(bucket)
            .order_by(bucket)
            .limit(MAX_TIMESERIES_POINTS)
        )
    ).all()
    by_agent_rows = (
        await db.execute(
            select(
                AgentTurn.agent,
                func.count(func.distinct(AgentTurn.agent_session_id)).label(
                    "session_count"
                ),
                func.count(AgentTurn.id).label("turn_count"),
                func.coalesce(func.sum(AgentTurn.total_tokens), 0).label(
                    "total_tokens"
                ),
                func.count(AgentTurn.id)
                .filter(successful)
                .label("success_count"),
                func.count(AgentTurn.id)
                .filter(failed)
                .label("failure_count"),
                func.count(AgentTurn.id)
                .filter(~successful, ~failed)
                .label("unknown_count"),
            )
            .select_from(AgentTurn)
            .join(AgentSession, AgentSession.id == AgentTurn.agent_session_id)
            .where(*_turn_where(filters))
            .group_by(AgentTurn.agent)
            .order_by(AgentTurn.agent)
        )
    ).all()
    return {
        "since": filters.since.isoformat(),
        "until": filters.until.isoformat(),
        "bucket": bucket_name,
        "points": [
            {
                "timestamp": row.bucket.isoformat(),
                "turn_count": int(row.turn_count or 0),
                "session_count": int(row.session_count or 0),
                "total_tokens": int(row.total_tokens or 0),
                "success_count": int(row.success_count or 0),
                "failure_count": int(row.failure_count or 0),
                "unknown_count": int(row.unknown_count or 0),
            }
            for row in rows
        ],
        "by_agent": [
            {
                "agent": row.agent,
                "session_count": int(row.session_count or 0),
                "turn_count": int(row.turn_count or 0),
                "total_tokens": int(row.total_tokens or 0),
                "success_count": int(row.success_count or 0),
                "failure_count": int(row.failure_count or 0),
                "unknown_count": int(row.unknown_count or 0),
            }
            for row in by_agent_rows
        ],
    }


def _percentile(column: Any, percentile: float):
    return func.percentile_cont(percentile).within_group(column)


async def latency(db: AsyncSession, filters: InsightFilters) -> dict[str, Any]:
    row = (
        await db.execute(
            select(
                func.count(AgentTurn.duration_ms).label("duration_count"),
                _percentile(AgentTurn.duration_ms, 0.5).label("duration_p50"),
                _percentile(AgentTurn.duration_ms, 0.95).label("duration_p95"),
                func.avg(AgentTurn.duration_ms).label("duration_avg"),
                func.count(AgentTurn.first_agent_latency_ms).label("first_count"),
                _percentile(AgentTurn.first_agent_latency_ms, 0.5).label(
                    "first_p50"
                ),
                _percentile(AgentTurn.first_agent_latency_ms, 0.95).label(
                    "first_p95"
                ),
                func.avg(AgentTurn.first_agent_latency_ms).label("first_avg"),
                func.count(AgentTurn.active_tool_ms).label("tool_count"),
                _percentile(AgentTurn.active_tool_ms, 0.5).label("tool_p50"),
                _percentile(AgentTurn.active_tool_ms, 0.95).label("tool_p95"),
                func.avg(AgentTurn.active_tool_ms).label("tool_avg"),
            )
            .select_from(AgentTurn)
            .join(AgentSession, AgentSession.id == AgentTurn.agent_session_id)
            .where(*_turn_where(filters))
        )
    ).one()
    histogram = (
        await db.execute(
            select(
                case(
                    (AgentTurn.duration_ms < 1_000, "under_1s"),
                    (AgentTurn.duration_ms < 5_000, "1s_5s"),
                    (AgentTurn.duration_ms < 30_000, "5s_30s"),
                    (AgentTurn.duration_ms < 120_000, "30s_2m"),
                    else_="over_2m",
                ).label("range"),
                func.count(AgentTurn.id).label("count"),
            )
            .select_from(AgentTurn)
            .join(AgentSession, AgentSession.id == AgentTurn.agent_session_id)
            .where(AgentTurn.duration_ms.is_not(None), *_turn_where(filters))
            .group_by("range")
        )
    ).all()
    confidence = _confidence_dict(
        await _group_counts(db, AgentTurn.metric_confidence, filters)
    )
    return {
        "since": filters.since.isoformat(),
        "until": filters.until.isoformat(),
        "summary": {
            "turn_duration_ms": {
                "count": int(row.duration_count or 0),
                "p50": _number(row.duration_p50),
                "p95": _number(row.duration_p95),
                "average": _number(row.duration_avg),
            },
            "first_agent_latency_ms": {
                "count": int(row.first_count or 0),
                "p50": _number(row.first_p50),
                "p95": _number(row.first_p95),
                "average": _number(row.first_avg),
            },
            "active_tool_ms": {
                "count": int(row.tool_count or 0),
                "p50": _number(row.tool_p50),
                "p95": _number(row.tool_p95),
                "average": _number(row.tool_avg),
            },
            "confidence": confidence,
        },
        "distribution": [
            {"range": name, "count": int(count)}
            for name, count in sorted(histogram, key=lambda item: str(item[0]))
        ],
    }


def _tool_duration():
    measured = (
        func.extract("epoch", ToolCall.completed_at - ToolCall.started_at) * 1000
    )
    return case(
        (
            and_(
                ToolCall.started_at.is_not(None),
                ToolCall.completed_at.is_not(None),
            ),
            measured,
        ),
        else_=None,
    )


async def tools(
    db: AsyncSession,
    filters: InsightFilters,
    *,
    tool_name: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    duration = _tool_duration()
    failed = or_(
        ToolCall.status.in_(("failed", "error")),
        and_(ToolCall.exit_code.is_not(None), ToolCall.exit_code != 0),
    )
    clauses = _tool_where(filters)
    if tool_name:
        clauses.append(ToolCall.tool_name == tool_name)
    if status:
        clauses.append(ToolCall.status == status)
    rows = (
        await db.execute(
            select(
                ToolCall.tool_name,
                ToolCall.tool_kind,
                func.count(ToolCall.id).label("call_count"),
                func.count(ToolCall.id).filter(failed).label("failure_count"),
                func.count(duration).label("duration_count"),
                func.avg(duration).label("average_ms"),
                _percentile(duration, 0.5).label("p50_ms"),
                _percentile(duration, 0.95).label("p95_ms"),
            )
            .select_from(ToolCall)
            .join(AgentSession, AgentSession.id == ToolCall.agent_session_id)
            .where(*clauses)
            .group_by(ToolCall.tool_name, ToolCall.tool_kind)
            .order_by(func.count(ToolCall.id).desc(), ToolCall.tool_name)
        )
    ).all()
    by_agent_rows = (
        await db.execute(
            select(
                ToolCall.agent,
                func.count(ToolCall.id).label("call_count"),
                func.count(ToolCall.id).filter(failed).label("failure_count"),
            )
            .select_from(ToolCall)
            .join(AgentSession, AgentSession.id == ToolCall.agent_session_id)
            .where(*clauses)
            .group_by(ToolCall.agent)
            .order_by(ToolCall.agent)
        )
    ).all()
    return {
        "items": [
            {
                "tool_name": row.tool_name,
                "tool_kind": row.tool_kind,
                "call_count": int(row.call_count),
                "failure_count": int(row.failure_count),
                "failure_rate": (
                    float(row.failure_count) / float(row.call_count)
                    if row.call_count
                    else 0.0
                ),
                "duration_count": int(row.duration_count),
                "average_ms": _number(row.average_ms),
                "p50_ms": _number(row.p50_ms),
                "p95_ms": _number(row.p95_ms),
            }
            for row in rows
        ],
        "by_agent": [
            {
                "agent": row.agent,
                "tool_call_count": int(row.call_count),
                "failure_count": int(row.failure_count),
            }
            for row in by_agent_rows
        ],
    }


async def skills(
    db: AsyncSession,
    filters: InsightFilters,
    *,
    confidence: str | None = None,
) -> dict[str, Any]:
    event_time = func.coalesce(
        SessionSkillEvidence.event_time,
        SessionSkillEvidence.created_at,
    )
    clauses: list[Any] = [
        event_time >= filters.since,
        event_time < filters.until,
        *_session_dimensions(filters),
    ]
    if confidence:
        clauses.append(SessionSkillEvidence.confidence == confidence)
    rows = (
        await db.execute(
            select(
                SessionSkillEvidence.skill_name,
                SkillDefinition.description,
                SkillDefinition.scope,
                SkillDefinition.enabled,
                SessionSkillEvidence.evidence_type,
                SessionSkillEvidence.confidence,
                func.count(SessionSkillEvidence.id).label("evidence_count"),
                func.max(event_time).label("last_seen_at"),
            )
            .select_from(SessionSkillEvidence)
            .join(
                AgentSession,
                AgentSession.id == SessionSkillEvidence.agent_session_id,
            )
            .outerjoin(
                SkillDefinition,
                SkillDefinition.id == SessionSkillEvidence.skill_id,
            )
            .where(*clauses)
            .group_by(
                SessionSkillEvidence.skill_name,
                SkillDefinition.description,
                SkillDefinition.scope,
                SkillDefinition.enabled,
                SessionSkillEvidence.evidence_type,
                SessionSkillEvidence.confidence,
            )
            .order_by(
                SessionSkillEvidence.skill_name,
                SessionSkillEvidence.evidence_type,
            )
        )
    ).all()
    session_counts = dict(
        (
            await db.execute(
                select(
                    SessionSkillEvidence.skill_name,
                    func.count(
                        func.distinct(SessionSkillEvidence.agent_session_id)
                    ),
                )
                .select_from(SessionSkillEvidence)
                .join(
                    AgentSession,
                    AgentSession.id == SessionSkillEvidence.agent_session_id,
                )
                .where(*clauses)
                .group_by(SessionSkillEvidence.skill_name)
            )
        ).all()
    )
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = grouped.setdefault(
            row.skill_name,
            {
                "skill_name": row.skill_name,
                "description": row.description,
                "scope": row.scope,
                "enabled": row.enabled,
                "evidence": {name: 0 for name in ("available", "candidate", "loaded", "inferred")},
                "confidence": {name: 0 for name in (*CONFIDENCE_LEVELS, "unknown")},
                "session_count": int(session_counts.get(row.skill_name, 0)),
                "last_seen_at": None,
            },
        )
        evidence_type = row.evidence_type or "unknown"
        item["evidence"][evidence_type] = (
            item["evidence"].get(evidence_type, 0) + int(row.evidence_count)
        )
        level = (
            row.confidence
            if row.confidence in CONFIDENCE_LEVELS
            else "unknown"
        )
        item["confidence"][level] += int(row.evidence_count)
        timestamp = _iso(row.last_seen_at)
        if timestamp and (item["last_seen_at"] is None or timestamp > item["last_seen_at"]):
            item["last_seen_at"] = timestamp
    return {"items": list(grouped.values())}


async def agents(db: AsyncSession, filters: InsightFilters) -> dict[str, Any]:
    child_session = aliased(AgentSession)
    parent_session = aliased(AgentSession)
    event_time = func.coalesce(
        ThreadEdge.event_time,
        ThreadEdge.created_at,
    )
    edge_clauses: list[Any] = [
        event_time >= filters.since,
        event_time < filters.until,
    ]
    if filters.workspace_id:
        edge_clauses.append(
            or_(
                child_session.workspace_id == filters.workspace_id,
                parent_session.workspace_id == filters.workspace_id,
            )
        )
    if filters.model:
        edge_clauses.append(
            or_(
                child_session.model == filters.model,
                parent_session.model == filters.model,
            )
        )
    if filters.agent == "stepgo":
        edge_clauses.append(
            or_(
                exists().where(
                    StepGoRun.linked_agent_session_id == child_session.id
                ),
                exists().where(
                    StepGoRun.linked_agent_session_id == parent_session.id
                ),
            )
        )
    elif filters.agent:
        edge_clauses.append(
            or_(
                child_session.agent == filters.agent,
                parent_session.agent == filters.agent,
            )
        )
    edge_rows = list(
        (
            await db.scalars(
                select(ThreadEdge)
                .outerjoin(
                    child_session,
                    child_session.thread_id == ThreadEdge.child_thread_id,
                )
                .outerjoin(
                    parent_session,
                    parent_session.thread_id == ThreadEdge.parent_thread_id,
                )
                .where(*edge_clauses)
                .order_by(event_time, ThreadEdge.id)
                .limit(MAX_AGENT_EDGES + 1)
            )
        ).all()
    )
    truncated = len(edge_rows) > MAX_AGENT_EDGES
    edge_rows = edge_rows[:MAX_AGENT_EDGES]
    thread_ids = {
        thread_id
        for row in edge_rows
        for thread_id in (row.parent_thread_id, row.child_thread_id)
    }
    if not thread_ids:
        return {"nodes": [], "edges": [], "truncated": False}
    sessions = list(
        (
            await db.scalars(
                select(AgentSession).where(AgentSession.thread_id.in_(thread_ids))
            )
        ).all()
    )
    goals = list(
        (
            await db.scalars(
                select(ThreadGoal).where(ThreadGoal.thread_id.in_(thread_ids))
            )
        ).all()
    )
    session_by_thread = {row.thread_id: row for row in sessions}
    goal_by_thread = {row.thread_id: row for row in goals}
    nodes = []
    for thread_id in sorted(thread_ids):
        session = session_by_thread.get(thread_id)
        goal = goal_by_thread.get(thread_id)
        nodes.append(
            {
                "thread_id": thread_id,
                "session_id": session.id if session else None,
                "title": (
                    session.display_title or session.ai_title or session.thread_name
                    if session
                    else None
                ),
                "model": session.model if session else None,
                "agent": session.agent if session else None,
                "workspace_id": session.workspace_id if session else None,
                "is_subagent": session.is_subagent if session else None,
                "outcome": goal.status if goal else None,
                "tokens_used": goal.tokens_used if goal else None,
            }
        )
    return {
        "nodes": nodes,
        "edges": [
            {
                "id": row.id,
                "source": row.parent_thread_id,
                "target": row.child_thread_id,
                "status": row.status,
                "event_time": _iso(row.event_time),
                "metadata": row.metadata_json,
            }
            for row in edge_rows
        ],
        "truncated": truncated,
    }


async def sources(
    db: AsyncSession,
    *,
    agent: str | None = None,
    source_kind: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    clauses: list[Any] = []
    if agent:
        clauses.append(LocalDataSource.agent == agent)
    if source_kind:
        clauses.append(LocalDataSource.source_kind == source_kind)
    if status:
        clauses.append(LocalDataSource.status == status)
    rows = list(
        (
            await db.scalars(
                select(LocalDataSource)
                .where(*clauses)
                .order_by(LocalDataSource.source_kind, LocalDataSource.source_path)
            )
        ).all()
    )
    return {
        "sources": [
            {
                "id": row.id,
                "agent": row.agent,
                "source_kind": row.source_kind,
                "source_path": row.source_path,
                "source_format": row.source_format,
                "parser_version": row.parser_version,
                "status": row.status,
                "record_count": row.record_count,
                "byte_size": row.byte_size,
                "coverage_started_at": _iso(row.coverage_started_at),
                "coverage_ended_at": _iso(row.coverage_ended_at),
                "backfill_cutoff_at": _iso(row.backfill_cutoff_at),
                "last_attempt_at": _iso(row.last_attempt_at),
                "last_success_at": _iso(row.last_success_at),
                "last_error_kind": row.last_error_kind,
                "last_error": row.last_error,
                "updated_at": _iso(row.updated_at),
            }
            for row in rows
        ]
    }


async def session_timeline(
    db: AsyncSession,
    session_id: str,
    *,
    limit: int = 500,
) -> dict[str, Any] | None:
    session = await db.get(AgentSession, session_id)
    if session is None:
        return None
    rows = list(
        (
            await db.scalars(
                select(ActivitySpan)
                .where(ActivitySpan.agent_session_id == session_id)
                .order_by(ActivitySpan.started_at, ActivitySpan.id)
                .limit(limit)
            )
        ).all()
    )
    return {
        "session_id": session_id,
        "spans": [
            {
                "id": row.id,
                "source_kind": row.source_kind,
                "source_record_id": row.source_record_id,
                "turn_id": row.turn_id,
                "thread_id": row.thread_id,
                "type": row.span_type,
                "name": row.name,
                "status": row.status,
                "started_at": row.started_at.isoformat(),
                "completed_at": _iso(row.completed_at),
                "duration_ms": row.duration_ms,
                "confidence": row.confidence,
                "evidence_id": row.evidence_id,
                "metadata": row.metadata_json,
            }
            for row in rows
        ],
    }


async def evidence_list(
    db: AsyncSession,
    filters: InsightFilters,
    *,
    source_id: str | None = None,
    category: str | None = None,
    level: str | None = None,
    thread_id: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    activity_time = func.coalesce(
        EvidenceRecord.recorded_at,
        EvidenceRecord.created_at,
    )
    clauses: list[Any] = [
        activity_time >= filters.since,
        activity_time < filters.until,
    ]
    if source_id:
        clauses.append(EvidenceRecord.source_id == source_id)
    if category:
        clauses.append(EvidenceRecord.category == category)
    if level:
        clauses.append(EvidenceRecord.level == level)
    if thread_id:
        clauses.append(EvidenceRecord.thread_id == thread_id)
    rows = (
        await db.execute(
            select(EvidenceRecord, LocalDataSource.source_kind)
            .join(LocalDataSource, LocalDataSource.id == EvidenceRecord.source_id)
            .where(*clauses)
            .order_by(activity_time.desc(), EvidenceRecord.id.desc())
            .limit(limit)
        )
    ).all()
    return {
        "items": [
            {
                "id": row.id,
                "source_id": row.source_id,
                "source_kind": source_kind,
                "source_record_id": row.source_record_id,
                "session_id": row.agent_session_id,
                "thread_id": row.thread_id,
                "turn_id": row.turn_id,
                "recorded_at": _iso(row.recorded_at),
                "category": row.category,
                "level": row.level,
                "target": redact_secrets(row.target),
                "summary": redact_secrets(row.summary),
                "raw_size": len(row.raw_text or ""),
            }
            for row, source_kind in rows
        ]
    }


async def evidence(
    db: AsyncSession,
    evidence_id: str,
) -> dict[str, Any] | None:
    row = await db.get(EvidenceRecord, evidence_id)
    if row is None:
        return None
    raw_json = row.raw_json
    if raw_json is None and row.agent_event_id is not None:
        event = await db.get(AgentEvent, row.agent_event_id)
        raw_json = event.raw_json if event is not None else None
    source = await db.get(LocalDataSource, row.source_id)
    return {
        "id": row.id,
        "source_id": row.source_id,
        "source_kind": source.source_kind if source else None,
        "source_record_id": row.source_record_id,
        "session_id": row.agent_session_id,
        "thread_id": row.thread_id,
        "turn_id": row.turn_id,
        "recorded_at": _iso(row.recorded_at),
        "category": row.category,
        "level": row.level,
        "target": redact_secrets(row.target),
        "summary": redact_secrets(row.summary),
        "raw_text": redact_secrets(row.raw_text),
        "raw_json": redact_value(raw_json),
        "raw_hash": row.raw_hash,
        "source_locator": redact_value(row.source_locator),
        "metadata": redact_value(row.metadata_json),
    }


def _flatten(prefix: str, value: Any) -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        result: list[tuple[str, Any]] = []
        for key, child in value.items():
            result.extend(_flatten(f"{prefix}.{key}" if prefix else key, child))
        return result
    return [(prefix, value)]


def csv_export(payload: dict[str, Any]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["dataset", "key", "value"])
    for dataset in ("overview",):
        for key, value in _flatten("", payload[dataset]):
            writer.writerow([dataset, key, value])
    for dataset in ("timeseries", "tools"):
        rows = payload[dataset].get(
            "points" if dataset == "timeseries" else "items", []
        )
        for index, item in enumerate(rows):
            for key, value in _flatten("", item):
                writer.writerow([dataset, f"{index}.{key}", value])
    return buffer.getvalue()


async def export_data(
    db: AsyncSession,
    filters: InsightFilters,
    *,
    format_name: str,
) -> dict[str, Any]:
    payload = {
        "overview": await overview(db, filters),
        "timeseries": await timeseries(db, filters),
        "tools": await tools(db, filters),
    }
    stamp = filters.until.strftime("%Y%m%dT%H%M%SZ")
    if format_name == "csv":
        return {
            "format": "csv",
            "filename": f"agent-observatory-insights-{stamp}.csv",
            "media_type": "text/csv",
            "content": csv_export(payload),
        }
    return {
        "format": "json",
        "filename": f"agent-observatory-insights-{stamp}.json",
        "media_type": "application/json",
        "content": payload,
    }


__all__ = [
    "InsightFilters",
    "MAX_TIMESERIES_POINTS",
    "resolve_range",
    "overview",
    "timeseries",
    "latency",
    "tools",
    "skills",
    "agents",
    "sources",
    "session_timeline",
    "evidence_list",
    "evidence",
    "export_data",
]
