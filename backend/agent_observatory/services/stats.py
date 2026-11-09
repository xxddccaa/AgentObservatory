"""Read-only aggregate statistics over indexed Codex sessions."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_observatory.db.models import AgentTurn

StatsRange = Literal["day", "week", "month"]

_RANGE_LENGTHS: dict[StatsRange, timedelta] = {
    "day": timedelta(days=1),
    "week": timedelta(days=7),
    "month": timedelta(days=30),
}


def range_bounds(
    range_name: StatsRange, *, now: datetime | None = None
) -> tuple[datetime, datetime]:
    """Return an aware UTC half-open interval for a requested range."""
    until = now or datetime.now(UTC)
    until = until.replace(tzinfo=UTC) if until.tzinfo is None else until.astimezone(UTC)
    return until - _RANGE_LENGTHS[range_name], until


async def token_usage(
    db: AsyncSession,
    range_name: StatsRange,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Aggregate session projections in ``[since, until)``.

    Prefer the durable session end/start timestamps so re-indexing an old
    rollout does not make its historical usage appear in today's window.
    ``updated_at`` remains a fallback for ongoing/legacy rows.
    """
    since, until = range_bounds(range_name, now=now)
    activity_time = func.coalesce(AgentTurn.completed_at, AgentTurn.started_at)
    query = select(
        func.coalesce(func.sum(AgentTurn.input_tokens), 0).label("input_tokens"),
        func.coalesce(func.sum(AgentTurn.cached_input_tokens), 0).label(
            "cached_input_tokens"
        ),
        func.coalesce(func.sum(AgentTurn.output_tokens), 0).label("output_tokens"),
        func.coalesce(func.sum(AgentTurn.reasoning_output_tokens), 0).label(
            "reasoning_output_tokens"
        ),
        func.coalesce(func.sum(AgentTurn.total_tokens), 0).label("total_tokens"),
        func.count(func.distinct(AgentTurn.agent_session_id)).label("session_count"),
        func.count(AgentTurn.id).label("turn_count"),
    ).where(activity_time >= since, activity_time < until)
    row = (await db.execute(query)).one()
    totals = {
        "input_tokens": int(row.input_tokens or 0),
        "cached_input_tokens": int(row.cached_input_tokens or 0),
        "output_tokens": int(row.output_tokens or 0),
        "reasoning_output_tokens": int(row.reasoning_output_tokens or 0),
        "total_tokens": int(row.total_tokens or 0),
        "session_count": int(row.session_count or 0),
        "turn_count": int(row.turn_count or 0),
    }
    return {
        "range": range_name,
        "since": since.isoformat(),
        "until": until.isoformat(),
        "totals": totals,
    }
