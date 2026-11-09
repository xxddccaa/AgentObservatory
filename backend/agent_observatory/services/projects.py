"""Project-level token analytics backed by PostgreSQL.

Projects intentionally reuse :class:`Workspace` as their identity.  The
indexer creates one workspace for each normalized, exact cwd; this service
therefore never performs parent/child directory inference.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_observatory.db.models import AgentSession, AgentTurn, Workspace

RANGES = ("all", "day", "week", "month")
MAX_TIMESERIES_POINTS = 500


def _activity_time():
    return func.coalesce(
        AgentTurn.completed_at,
        AgentTurn.started_at,
        AgentTurn.created_at,
    )


def _joined_first_activity_time():
    """Earliest known timestamp across the session and each joined turn."""
    return func.least(
        _activity_time(),
        AgentSession.start_time,
        AgentSession.end_time,
        AgentSession.last_activity_at,
    )


def _joined_last_activity_time():
    """Latest known timestamp across the session and each joined turn."""
    return func.greatest(
        _activity_time(),
        func.coalesce(
            AgentSession.last_activity_at,
            AgentSession.end_time,
            AgentSession.start_time,
        ),
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _tokens(row: Any, prefix: str = "") -> dict[str, int]:
    def value(name: str) -> int:
        raw = getattr(row, f"{prefix}{name}", 0)
        return int(raw or 0)

    return {
        "input_tokens": value("input_tokens"),
        "cached_input_tokens": value("cached_input_tokens"),
        "output_tokens": value("output_tokens"),
        "reasoning_output_tokens": value("reasoning_output_tokens"),
        "total_tokens": value("total_tokens"),
    }


def resolve_window(
    range_name: str,
    *,
    now: datetime | None = None,
) -> tuple[datetime | None, datetime, str]:
    """Return ``(since, until, normalized_range)`` using UTC half-open windows."""
    if range_name not in RANGES:
        raise ValueError("range must be all, day, week, or month")
    until = now or datetime.now(UTC)
    until = until.replace(tzinfo=UTC) if until.tzinfo is None else until.astimezone(UTC)
    if range_name == "all":
        return None, until, range_name
    durations = {"day": timedelta(days=1), "week": timedelta(days=7), "month": timedelta(days=30)}
    return until - durations[range_name], until, range_name


def _range_clause(since: datetime | None, until: datetime):
    activity = _activity_time()
    if since is None:
        return activity < until
    return and_(activity >= since, activity < until)


def _aggregate_columns(since: datetime | None, until: datetime, prefix: str = ""):
    """Build token aggregate expressions, optionally constrained to a window."""
    condition = _range_clause(since, until)
    kwargs = {}
    for name in (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    ):
        column = getattr(AgentTurn, name)
        aggregate = func.sum(column).filter(condition)
        aggregate = func.coalesce(aggregate, 0)
        kwargs[f"{prefix}{name}"] = aggregate.label(f"{prefix}{name}")
    return kwargs


async def list_projects(
    db: AsyncSession,
    *,
    limit: int = 50,
    offset: int = 0,
    q: str | None = None,
    range_name: str = "month",
    agent: str | None = None,
    model: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    since, until, normalized_range = resolve_window(range_name, now=now)
    session_join = [AgentSession.workspace_id == Workspace.id]
    if agent:
        session_join.append(AgentSession.agent == agent)
    if model:
        session_join.append(AgentSession.model == model)
    turn_join = AgentTurn.agent_session_id == AgentSession.id
    all_columns = _aggregate_columns(None, until, "all_")
    selected_columns = _aggregate_columns(since, until, "selected_")
    stmt = (
        select(
            Workspace.id,
            Workspace.display_name,
            Workspace.path,
            Workspace.is_allowed,
            Workspace.workspace_kind,
            Workspace.workspace_confidence,
            func.max(_joined_last_activity_time()).label("last_active_at"),
            func.count(func.distinct(AgentSession.id)).label("session_count"),
            func.count(AgentTurn.id).label("turn_count"),
            *all_columns.values(),
            *selected_columns.values(),
        )
        .select_from(Workspace)
        .outerjoin(AgentSession, and_(*session_join))
        .outerjoin(AgentTurn, turn_join)
        .group_by(Workspace.id)
    )
    query = (q or "").strip()
    if query:
        escaped = query.replace("%", r"\%").replace("_", r"\_")
        pattern = f"%{escaped}%"
        stmt = stmt.where(
            or_(
                Workspace.display_name.ilike(pattern, escape="\\"),
                Workspace.path.ilike(pattern, escape="\\"),
            )
        )
    if agent or model:
        matching = [AgentSession.workspace_id == Workspace.id]
        if agent:
            matching.append(AgentSession.agent == agent)
        if model:
            matching.append(AgentSession.model == model)
        stmt = stmt.where(exists().where(and_(*matching)).correlate(Workspace))

    # Total is computed independently of pagination; the same workspace search
    # predicate is reused to avoid loading every project into Python.
    count_stmt = select(func.count()).select_from(Workspace)
    if query:
        count_stmt = count_stmt.where(
            or_(
                Workspace.display_name.ilike(pattern, escape="\\"),
                Workspace.path.ilike(pattern, escape="\\"),
            )
        )
    if agent or model:
        matching = [AgentSession.workspace_id == Workspace.id]
        if agent:
            matching.append(AgentSession.agent == agent)
        if model:
            matching.append(AgentSession.model == model)
        count_stmt = count_stmt.where(exists().where(and_(*matching)).correlate(Workspace))
    total = int((await db.scalar(count_stmt)) or 0)
    rows = (
        await db.execute(
            stmt.order_by(
                func.max(_joined_last_activity_time())
                .desc()
                .nullslast(),
                Workspace.id.desc(),
            )
            .offset(offset)
            .limit(limit)
        )
    ).all()
    items = []
    for row in rows:
        items.append(
            {
                "id": row.id,
                "display_name": row.display_name,
                "path": row.path,
                "is_allowed": bool(row.is_allowed),
                "workspace_kind": row.workspace_kind,
                "workspace_confidence": row.workspace_confidence,
                "last_active_at": _iso(row.last_active_at),
                "session_count": int(row.session_count or 0),
                "turn_count": int(row.turn_count or 0),
                "all_time": _tokens(row, "all_"),
                "selected_range": {
                    "range": normalized_range,
                    **_tokens(row, "selected_"),
                },
            }
        )
    return {
        "items": items,
        "total": total,
        "range": normalized_range,
        "since": _iso(since),
        "until": until.isoformat(),
    }


async def get_project(
    db: AsyncSession,
    workspace_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    workspace = await db.get(Workspace, workspace_id)
    if workspace is None:
        return None
    until = (now or datetime.now(UTC)).astimezone(UTC)
    session_count, turn_count = (
        await db.execute(
            select(
                func.count(func.distinct(AgentSession.id)),
                func.count(AgentTurn.id),
            )
            .select_from(AgentSession)
            .outerjoin(AgentTurn, AgentTurn.agent_session_id == AgentSession.id)
            .where(AgentSession.workspace_id == workspace_id)
        )
    ).one()
    all_row = (
        await db.execute(
            select(*_aggregate_columns(None, until).values())
            .select_from(AgentTurn)
            .join(AgentSession, AgentSession.id == AgentTurn.agent_session_id)
            .where(AgentSession.workspace_id == workspace_id)
        )
    ).one()
    totals: dict[str, dict[str, int]] = {"all_time": _tokens(all_row)}
    for name in ("day", "week", "month"):
        since, _, _ = resolve_window(name, now=until)
        row = (
            await db.execute(
                select(*_aggregate_columns(since, until).values())
                .select_from(AgentTurn)
                .join(AgentSession, AgentSession.id == AgentTurn.agent_session_id)
                .where(AgentSession.workspace_id == workspace_id)
            )
        ).one()
        totals[name] = _tokens(row)

    first_last = (
        await db.execute(
            select(
                func.min(_joined_first_activity_time()).label("first_active_at"),
                func.max(_joined_last_activity_time()).label("last_active_at"),
            )
            .select_from(AgentSession)
            .outerjoin(AgentTurn, AgentTurn.agent_session_id == AgentSession.id)
            .where(AgentSession.workspace_id == workspace_id)
        )
    ).one()
    by_agent_rows = (
        await db.execute(
            select(
                AgentTurn.agent.label("agent"),
                func.count(func.distinct(AgentTurn.agent_session_id)).label("session_count"),
                func.count(AgentTurn.id).label("turn_count"),
                *(_aggregate_columns(None, until).values()),
            )
            .select_from(AgentTurn)
            .join(AgentSession, AgentSession.id == AgentTurn.agent_session_id)
            .where(AgentSession.workspace_id == workspace_id)
            .group_by(AgentTurn.agent)
            .order_by(AgentTurn.agent)
        )
    ).all()
    by_model_rows = (
        await db.execute(
            select(
                AgentSession.model.label("model"),
                func.count(func.distinct(AgentTurn.agent_session_id)).label("session_count"),
                func.count(AgentTurn.id).label("turn_count"),
                *(_aggregate_columns(None, until).values()),
            )
            .select_from(AgentTurn)
            .join(AgentSession, AgentSession.id == AgentTurn.agent_session_id)
            .where(AgentSession.workspace_id == workspace_id)
            .group_by(AgentSession.model)
            .order_by(AgentSession.model)
        )
    ).all()

    def split(row: Any, key: str) -> dict[str, Any]:
        return {
            key: getattr(row, key) or "unknown",
            "session_count": int(row.session_count or 0),
            "turn_count": int(row.turn_count or 0),
            **_tokens(row),
        }

    return {
        "project": {
            "id": workspace.id,
            "display_name": workspace.display_name,
            "path": workspace.path,
            "is_allowed": bool(workspace.is_allowed),
            "workspace_kind": workspace.workspace_kind,
            "workspace_confidence": workspace.workspace_confidence,
            "first_active_at": _iso(first_last.first_active_at),
            "last_active_at": _iso(first_last.last_active_at),
        },
        "totals": totals,
        "counts": {"session_count": int(session_count or 0), "turn_count": int(turn_count or 0)},
        "by_agent": [split(row, "agent") for row in by_agent_rows],
        "by_model": [split(row, "model") for row in by_model_rows],
    }


async def project_timeseries(
    db: AsyncSession,
    workspace_id: str,
    *,
    range_name: str = "day",
    now: datetime | None = None,
) -> dict[str, Any] | None:
    if range_name not in {"day", "week", "month"}:
        raise ValueError("range must be day, week, or month")
    if await db.get(Workspace, workspace_id) is None:
        return None
    since, until, _ = resolve_window(range_name, now=now)
    bucket_name = "hour" if range_name == "day" else "day"
    activity = _activity_time()
    # The third PostgreSQL argument makes truncation deterministic regardless
    # of the database/session timezone; API timestamps are always UTC.
    bucket = func.date_trunc(bucket_name, activity, "UTC").label("bucket")
    stmt = (
        select(
            bucket,
            func.count(AgentTurn.id).label("turn_count"),
            func.count(func.distinct(AgentTurn.agent_session_id)).label("session_count"),
            *(_aggregate_columns(None, until).values()),
        )
        .select_from(AgentTurn)
        .join(AgentSession, AgentSession.id == AgentTurn.agent_session_id)
        .where(
            AgentSession.workspace_id == workspace_id,
            activity >= since,
            activity < until,
        )
        .group_by(bucket)
        .order_by(bucket)
        .limit(MAX_TIMESERIES_POINTS)
    )
    rows = (await db.execute(stmt)).all()
    return {
        "since": since.isoformat() if since else None,
        "until": until.isoformat(),
        "bucket": bucket_name,
        "points": [
            {
                "timestamp": _iso(row.bucket),
                "session_count": int(row.session_count or 0),
                "turn_count": int(row.turn_count or 0),
                **_tokens(row),
            }
            for row in rows
        ],
    }


timeseries = project_timeseries

__all__ = [
    "get_project",
    "list_projects",
    "project_timeseries",
    "resolve_window",
    "timeseries",
]
