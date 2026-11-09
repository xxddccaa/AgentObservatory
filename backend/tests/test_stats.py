from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from agent_observatory.db.models import AgentSession, AgentTurn
from agent_observatory.services.stats import token_usage


def _session(session_id: str, when: datetime, **values: int) -> AgentSession:
    return AgentSession(
        id=session_id,
        thread_id=f"thread-{session_id}",
        rollout_path=f"/tmp/{session_id}.jsonl",
        rollout_real_path=f"/real/{session_id}.jsonl",
        source_identity=f"test:{session_id}",
        source_path=f"/real/{session_id}.jsonl",
        source_format="jsonl",
        updated_at=when,
        start_time=when,
        **values,
    )


@pytest.mark.anyio
async def test_token_usage_is_zero_safe(db_session: AsyncSession) -> None:
    now = datetime(2026, 8, 22, 12, tzinfo=UTC)
    result = await token_usage(db_session, "day", now=now)
    assert result["range"] == "day"
    assert result["since"] == "2026-08-21T12:00:00+00:00"
    assert result["until"] == "2026-08-22T12:00:00+00:00"
    assert result["totals"] == {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "total_tokens": 0,
        "session_count": 0,
        "turn_count": 0,
    }


@pytest.mark.anyio
async def test_token_usage_aggregates_sessions_in_window(db_session: AsyncSession) -> None:
    now = datetime(2026, 8, 22, 12, tzinfo=UTC)
    db_session.add_all(
        [
            _session(
                "in-window",
                now - timedelta(hours=2),
            ),
            _session(
                "old",
                now - timedelta(days=8),
                input_tokens=900,
                output_tokens=900,
                total_tokens=1800,
                turn_count=9,
            ),
        ]
    )
    db_session.add_all([
        AgentTurn(id="turn-in-1", agent_session_id="in-window", turn_index=0,
                  started_at=now - timedelta(hours=2), completed_at=now - timedelta(hours=1),
                  input_tokens=100, cached_input_tokens=25, output_tokens=40,
                  reasoning_output_tokens=10, total_tokens=140),
        AgentTurn(id="turn-in-2", agent_session_id="in-window", turn_index=1,
                  started_at=now - timedelta(hours=1), completed_at=now - timedelta(minutes=30),
                  input_tokens=0, output_tokens=0, total_tokens=0),
        AgentTurn(id="turn-old", agent_session_id="old", turn_index=0,
                  started_at=now - timedelta(days=8), completed_at=now - timedelta(days=8),
                  input_tokens=900, output_tokens=900, total_tokens=1800),
    ])
    await db_session.flush()

    result = await token_usage(db_session, "week", now=now)
    assert result["totals"] == {
        "input_tokens": 100,
        "cached_input_tokens": 25,
        "output_tokens": 40,
        "reasoning_output_tokens": 10,
        "total_tokens": 140,
        "session_count": 1,
        "turn_count": 2,
    }
