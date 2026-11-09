from pathlib import Path

import pytest
from sqlalchemy import select

from agent_observatory.db.models import AgentEvent
from agent_observatory.workers.indexer.adapters import PlanAction, SourcePlan
from agent_observatory.workers.indexer.generic import GenericJsonlSource


@pytest.mark.asyncio
async def test_generic_retains_malformed_lines(tmp_path: Path, db_session):
    p = tmp_path / "events.jsonl"
    p.write_bytes(
        b'{"role":"user","text":"ok"}\nnot-json\n{"role":"assistant","text":"done"}\npartial'
    )
    adapter = GenericJsonlSource(tmp_path, agent="custom")
    source = adapter.discover()[0]
    session = await adapter.ingest(db_session, SourcePlan(source, PlanAction.REBUILD, {}))
    assert session.agent == "custom"
    events = (await db_session.scalars(select(AgentEvent).order_by(AgentEvent.event_index))).all()
    assert any(e.normalized_type == "parse_error" for e in events)
    assert len(events) == 3
