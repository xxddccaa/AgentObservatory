import json
from pathlib import Path

import pytest
from sqlalchemy import select

from agent_observatory.db.models import CodexMessage
from agent_observatory.workers.indexer.adapters import PlanAction, SourcePlan
from agent_observatory.workers.indexer.opencode import OpenCodeJsonSource


def test_absent_root(tmp_path: Path):
    assert OpenCodeJsonSource(tmp_path / "missing").discover() == []


@pytest.mark.asyncio
async def test_opencode_ingest_messages(tmp_path: Path, db_session):
    root = tmp_path / "store"
    (root / "session").mkdir(parents=True)
    p = root / "session" / "s1.json"
    p.write_text(json.dumps({"id": "s1", "title": "Demo", "model": "x"}))
    (root / "message" / "s1").mkdir(parents=True)
    (root / "message" / "s1" / "m1.json").write_text(json.dumps({"role": "user", "content": "hi"}))
    adapter = OpenCodeJsonSource(root)
    source = adapter.discover()[0]
    session = await adapter.ingest(db_session, SourcePlan(source, PlanAction.REBUILD, {}))
    assert session.display_title == "Demo"
    assert (await db_session.scalars(select(CodexMessage))).first().text == "hi"
