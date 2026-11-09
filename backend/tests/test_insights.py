from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from agent_observatory.api.insights import router as insights_router
from agent_observatory.db.models import (
    ActivitySpan,
    AgentEvent,
    AgentSession,
    AgentTurn,
    AuditLog,
    EvidenceRecord,
    LocalDataSource,
    SessionSkillEvidence,
    SkillDefinition,
    StepGoRun,
    ThreadEdge,
    ThreadGoal,
    ToolCall,
)
from agent_observatory.services import insights as insights_service

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _mount_insights(app) -> None:
    if not any(
        getattr(route, "path", "").startswith("/api/insights")
        for route in app.routes
    ):
        before = len(app.routes)
        app.include_router(insights_router)
        added = app.router.routes[before:]
        del app.router.routes[before:]
        fallback_index = next(
            index
            for index, route in enumerate(app.router.routes)
            if getattr(route, "path", "") == "/{path:path}"
        )
        app.router.routes[fallback_index:fallback_index] = added


def _session(
    session_id: str,
    *,
    thread_id: str | None = None,
    model: str = "gpt-5",
    workspace_id: str | None = None,
    is_subagent: bool = False,
    agent: str = "codex",
) -> AgentSession:
    return AgentSession(
        id=session_id,
        thread_id=thread_id or f"thread-{session_id}",
        rollout_path=f"/tmp/{session_id}.jsonl",
        rollout_real_path=f"/real/{session_id}.jsonl",
        source_identity=f"test:{session_id}",
        agent=agent,
        source_path=f"/real/{session_id}.jsonl",
        source_format="jsonl",
        model=model,
        workspace_id=workspace_id,
        start_time=NOW - timedelta(hours=2),
        end_time=NOW - timedelta(hours=1),
        updated_at=NOW - timedelta(hours=1),
        is_subagent=is_subagent,
    )


async def _seed_insights(db: AsyncSession) -> None:
    source = LocalDataSource(
        id="source-1",
        source_kind="thread_history",
        source_identity="sqlite:/tmp/thread_history.sqlite",
        source_path="/tmp/thread_history.sqlite",
        source_format="sqlite",
        parser_version="1",
        status="ready",
        record_count=12,
        byte_size=4096,
        coverage_started_at=NOW - timedelta(days=4),
        coverage_ended_at=NOW,
        last_success_at=NOW,
    )
    parent = _session("session-parent", thread_id="thread-parent")
    child = _session(
        "session-child",
        thread_id="thread-child",
        is_subagent=True,
        agent="stepcode",
    )
    db.add_all([source, parent, child])
    await db.flush()
    db.add_all(
        [
            AgentTurn(
                id="turn-1",
                agent="codex",
                agent_session_id=parent.id,
                turn_index=0,
                status="completed",
                started_at=NOW - timedelta(minutes=50),
                completed_at=NOW - timedelta(minutes=49),
                duration_ms=100,
                first_agent_latency_ms=10,
                active_tool_ms=50,
                total_tokens=100,
                input_tokens=60,
                cached_input_tokens=10,
                output_tokens=40,
                reasoning_output_tokens=5,
                validation_status="passed",
                outcome="completed",
                metric_confidence="exact",
            ),
            AgentTurn(
                id="turn-2",
                agent="codex",
                agent_session_id=parent.id,
                turn_index=1,
                status="completed",
                started_at=NOW - timedelta(minutes=40),
                completed_at=NOW - timedelta(minutes=39),
                duration_ms=200,
                first_agent_latency_ms=20,
                active_tool_ms=100,
                total_tokens=200,
                input_tokens=120,
                output_tokens=80,
                reasoning_output_tokens=10,
                validation_status="failed",
                outcome="failed",
                metric_confidence="derived",
            ),
            AgentTurn(
                id="turn-3",
                agent="stepcode",
                agent_session_id=child.id,
                turn_index=0,
                status="completed",
                started_at=NOW - timedelta(minutes=30),
                completed_at=NOW - timedelta(minutes=29),
                duration_ms=300,
                first_agent_latency_ms=30,
                active_tool_ms=150,
                total_tokens=300,
                input_tokens=180,
                output_tokens=120,
                reasoning_output_tokens=15,
                validation_status="unknown",
                outcome="unknown",
                metric_confidence="inferred",
            ),
        ]
    )
    db.add_all(
        [
            ToolCall(
                id="tool-1",
                agent="codex",
                agent_session_id=parent.id,
                turn_id="turn-1",
                call_id="call-1",
                tool_name="exec_command",
                tool_kind="shell",
                started_at=NOW - timedelta(minutes=50),
                completed_at=NOW - timedelta(minutes=50) + timedelta(milliseconds=100),
                status="completed",
                exit_code=0,
            ),
            ToolCall(
                id="tool-2",
                agent="codex",
                agent_session_id=parent.id,
                turn_id="turn-2",
                call_id="call-2",
                tool_name="exec_command",
                tool_kind="shell",
                started_at=NOW - timedelta(minutes=40),
                completed_at=NOW - timedelta(minutes=40) + timedelta(milliseconds=300),
                status="failed",
                exit_code=1,
            ),
        ]
    )
    event = AgentEvent(
        agent_session_id=parent.id,
        event_index=0,
        line_number=1,
        byte_offset=0,
        event_timestamp=NOW - timedelta(minutes=49),
        top_level_type="response_item",
        payload_type="message",
        normalized_type="message",
        raw_json={"type": "response_item", "payload": {"text": "full evidence"}},
        raw_hash="event-hash",
        is_known=True,
    )
    db.add(event)
    await db.flush()
    evidence = EvidenceRecord(
        id="evidence-1",
        source_id=source.id,
        source_record_id="row:1",
        agent_session_id=parent.id,
        agent_event_id=event.id,
        thread_id=parent.thread_id,
        turn_id="turn-1",
        recorded_at=NOW - timedelta(minutes=49),
        category="turn",
        summary="Turn completed",
        raw_hash="evidence-hash",
        source_locator={"row": 1},
    )
    skill = SkillDefinition(
        id="skill-1",
        name="imagegen",
        description="Generate images",
        source_path="/skills/imagegen/SKILL.md",
        scope="system",
        content_hash="skill-hash",
        enabled=True,
        first_seen_at=NOW - timedelta(days=2),
        last_seen_at=NOW,
    )
    db.add_all([evidence, skill])
    await db.flush()
    db.add_all(
        [
            SessionSkillEvidence(
                id="skill-evidence-1",
                agent_session_id=parent.id,
                skill_id=skill.id,
                evidence_id=evidence.id,
                turn_id="turn-1",
                skill_name="imagegen",
                evidence_type="available",
                confidence="exact",
                source_record_id="skill:available",
                event_time=NOW - timedelta(minutes=49),
            ),
            SessionSkillEvidence(
                id="skill-evidence-2",
                agent_session_id=parent.id,
                skill_id=skill.id,
                evidence_id=evidence.id,
                turn_id="turn-1",
                skill_name="imagegen",
                evidence_type="loaded",
                confidence="derived",
                source_record_id="skill:loaded",
                event_time=NOW - timedelta(minutes=48),
            ),
            ActivitySpan(
                id="span-2",
                agent="codex",
                agent_session_id=parent.id,
                evidence_id=evidence.id,
                thread_id=parent.thread_id,
                turn_id="turn-1",
                span_type="tool",
                name="exec_command",
                status="completed",
                started_at=NOW - timedelta(minutes=48),
                completed_at=NOW - timedelta(minutes=47),
                duration_ms=60_000,
                confidence="exact",
            ),
            ActivitySpan(
                id="span-1",
                agent="codex",
                agent_session_id=parent.id,
                thread_id=parent.thread_id,
                turn_id="turn-1",
                span_type="reasoning",
                name="reasoning",
                status="completed",
                started_at=NOW - timedelta(minutes=49),
                completed_at=NOW - timedelta(minutes=48),
                duration_ms=60_000,
                confidence="derived",
            ),
            ThreadEdge(
                id="edge-1",
                source_id=source.id,
                parent_thread_id=parent.thread_id,
                child_thread_id=child.thread_id,
                status="completed",
                event_time=NOW - timedelta(minutes=30),
            ),
            ThreadGoal(
                thread_id=child.thread_id,
                source_id=source.id,
                goal_id="goal-1",
                objective="Inspect a bounded subsystem",
                status="complete",
                token_budget=1000,
                tokens_used=300,
                time_used_seconds=60,
                source_created_at_ms=int(
                    (NOW - timedelta(minutes=31)).timestamp() * 1000
                ),
                updated_at_ms=int((NOW - timedelta(minutes=29)).timestamp() * 1000),
            ),
        ]
    )
    await db.flush()


@pytest.mark.anyio
async def test_insights_require_authentication(client: AsyncClient) -> None:
    response = await client.get("/api/insights/overview")
    assert response.status_code == 401


@pytest.mark.anyio
async def test_viewer_can_read_zero_safe_overview(
    as_viewer: AsyncClient,
) -> None:
    response = await as_viewer.get(
        "/api/insights/overview",
        params={
            "from": "2026-01-01T00:00:00+00:00",
            "to": "2026-01-02T00:00:00+00:00",
        },
    )
    assert response.status_code == 200
    assert response.json()["totals"] == {
        "session_count": 0,
        "turn_count": 0,
        "total_tokens": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    assert response.json()["quality"]["confidence"] == {
        "exact": 0,
        "derived": 0,
        "inferred": 0,
        "unknown": 0,
    }


@pytest.mark.anyio
async def test_aggregates_latency_tools_skills_and_timeseries(
    as_viewer: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await _seed_insights(db_session)
    params = {
        "from": (NOW - timedelta(days=1)).isoformat(),
        "to": NOW.isoformat(),
    }

    overview = (await as_viewer.get("/api/insights/overview", params=params)).json()
    assert overview["totals"]["session_count"] == 2
    assert overview["totals"]["turn_count"] == 3
    assert overview["totals"]["total_tokens"] == 600
    assert overview["quality"]["outcomes"] == {
        "completed": 1,
        "failed": 1,
        "unknown": 1,
    }
    assert overview["quality"]["confidence"] == {
        "exact": 1,
        "derived": 1,
        "inferred": 1,
        "unknown": 0,
    }
    assert overview["by_agent"] == [
        {
            "agent": "codex",
            "session_count": 1,
            "turn_count": 2,
            "total_tokens": 300,
            "tool_call_count": 2,
            "failure_count": 1,
        },
        {
            "agent": "stepcode",
            "session_count": 1,
            "turn_count": 1,
            "total_tokens": 300,
            "tool_call_count": 0,
            "failure_count": 0,
        },
    ]
    stepcode_overview = (
        await as_viewer.get(
            "/api/insights/overview", params={**params, "agent": "stepcode"}
        )
    ).json()
    assert stepcode_overview["totals"]["turn_count"] == 1
    assert stepcode_overview["totals"]["total_tokens"] == 300
    stepgo_overview = (
        await as_viewer.get(
            "/api/insights/overview", params={**params, "agent": "stepgo"}
        )
    ).json()
    assert stepgo_overview["totals"]["session_count"] == 0
    assert stepgo_overview["totals"]["total_tokens"] == 0

    timeseries = (
        await as_viewer.get("/api/insights/timeseries", params=params)
    ).json()
    assert len(timeseries["points"]) <= 500
    assert sum(point["turn_count"] for point in timeseries["points"]) == 3
    assert sum(point["success_count"] for point in timeseries["points"]) == 1
    assert sum(point["failure_count"] for point in timeseries["points"]) == 1
    assert sum(point["unknown_count"] for point in timeseries["points"]) == 1
    assert {row["agent"] for row in timeseries["by_agent"]} == {
        "codex",
        "stepcode",
    }

    latency = (await as_viewer.get("/api/insights/latency", params=params)).json()
    assert latency["summary"]["turn_duration_ms"] == {
        "count": 3,
        "p50": 200.0,
        "p95": 290.0,
        "average": 200.0,
    }
    assert sum(item["count"] for item in latency["distribution"]) == 3

    tools = (await as_viewer.get("/api/insights/tools", params=params)).json()
    assert tools["items"][0]["call_count"] == 2
    assert tools["items"][0]["failure_count"] == 1
    assert tools["items"][0]["p50_ms"] == pytest.approx(200.0)
    assert tools["by_agent"] == [
        {"agent": "codex", "tool_call_count": 2, "failure_count": 1}
    ]


@pytest.mark.anyio
async def test_insights_stepgo_filter_counts_native_metrics_once(
    as_viewer: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await _seed_insights(db_session)
    db_session.add(
        StepGoRun(
            id="run-parent",
            host_run_id="run-parent",
            source_identity="stepgo:run-parent",
            source_path="/tmp/stepgo/run-parent.jsonl",
            agent="codex",
            status="completed",
            link_status="linked",
            linked_agent_session_id="session-parent",
            start_time=NOW - timedelta(hours=2),
            end_time=NOW - timedelta(hours=1),
            exit_code=0,
        )
    )
    await db_session.flush()
    params = {
        "from": (NOW - timedelta(days=1)).isoformat(),
        "to": NOW.isoformat(),
    }

    overview = (
        await as_viewer.get(
            "/api/insights/overview", params={**params, "agent": "stepgo"}
        )
    ).json()
    assert overview["totals"]["session_count"] == 1
    assert overview["totals"]["turn_count"] == 2
    assert overview["totals"]["total_tokens"] == 300
    assert [row["agent"] for row in overview["by_agent"]] == ["codex"]

    all_agents = (
        await as_viewer.get("/api/insights/overview", params=params)
    ).json()
    assert all_agents["totals"]["session_count"] == 2
    assert all_agents["totals"]["total_tokens"] == 600

    skills = (await as_viewer.get("/api/insights/skills", params=params)).json()
    assert skills["items"][0]["evidence"]["available"] == 1
    assert skills["items"][0]["evidence"]["loaded"] == 1
    assert skills["items"][0]["confidence"]["exact"] == 1
    assert skills["items"][0]["confidence"]["derived"] == 1


@pytest.mark.anyio
async def test_timeseries_buckets_are_utc_when_database_timezone_is_not_utc(
    db_session: AsyncSession,
) -> None:
    await _seed_insights(db_session)
    await db_session.execute(text("SET LOCAL TIME ZONE 'America/Los_Angeles'"))
    filters = insights_service.resolve_range(
        NOW - timedelta(days=1),
        NOW,
    )

    payload = await insights_service.timeseries(db_session, filters)

    timestamps = [
        datetime.fromisoformat(point["timestamp"]).astimezone(UTC)
        for point in payload["points"]
    ]
    assert timestamps == [datetime(2026, 8, 25, 11, tzinfo=UTC)]


@pytest.mark.anyio
async def test_sources_agent_graph_and_ordered_timeline(
    as_operator: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await _seed_insights(db_session)
    params = {
        "from": (NOW - timedelta(days=1)).isoformat(),
        "to": NOW.isoformat(),
    }
    sources = (await as_operator.get("/api/insights/sources")).json()
    assert sources["sources"][0]["status"] == "ready"
    assert sources["sources"][0]["agent"] == "codex"
    assert (
        await as_operator.get(
            "/api/insights/sources", params={"agent": "stepcode"}
        )
    ).json()["sources"] == []
    assert sources["sources"][0]["coverage_ended_at"] == NOW.isoformat()

    graph = (await as_operator.get("/api/insights/agents", params=params)).json()
    assert {node["thread_id"] for node in graph["nodes"]} == {
        "thread-parent",
        "thread-child",
    }
    assert graph["edges"][0]["source"] == "thread-parent"
    child = next(node for node in graph["nodes"] if node["thread_id"] == "thread-child")
    assert child["outcome"] == "complete"

    timeline = (
        await as_operator.get("/api/insights/sessions/session-parent/timeline")
    ).json()
    assert timeline["session_id"] == "session-parent"
    assert [span["id"] for span in timeline["spans"]] == ["span-1", "span-2"]
    assert timeline["spans"][1]["evidence_id"] == "evidence-1"


@pytest.mark.anyio
async def test_evidence_and_exports_are_audited(
    as_viewer: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await _seed_insights(db_session)

    listed = await as_viewer.get(
        "/api/insights/evidence",
        params={
            "source_id": "source-1",
            "from": (NOW - timedelta(days=1)).isoformat(),
            "to": NOW.isoformat(),
        },
    )
    assert listed.status_code == 200
    assert listed.json()["items"][0]["id"] == "evidence-1"
    assert "raw_json" not in listed.json()["items"][0]

    evidence = await as_viewer.get("/api/insights/evidence/evidence-1")
    assert evidence.status_code == 200
    assert evidence.json()["raw_json"]["payload"]["text"] == "full evidence"

    exported = await as_viewer.get(
        "/api/insights/export",
        params={
            "format": "csv",
            "from": (NOW - timedelta(days=1)).isoformat(),
            "to": NOW.isoformat(),
        },
    )
    assert exported.status_code == 200
    assert exported.json()["media_type"] == "text/csv"
    assert exported.json()["content"].startswith("dataset,key,value")

    rows = list(
        (
            await db_session.scalars(
                select(AuditLog)
                .where(
                    AuditLog.action.in_(
                        ("insights.evidence.viewed", "insights.exported")
                    )
                )
                .order_by(AuditLog.id)
            )
        ).all()
    )
    assert [row.action for row in rows] == [
        "insights.evidence.viewed",
        "insights.exported",
    ]
    assert all(row.actor_id for row in rows)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("path", "params"),
    [
        (
            "/api/insights/overview",
            {
                "from": "2026-08-26T00:00:00+00:00",
                "to": "2026-08-25T00:00:00+00:00",
            },
        ),
        ("/api/insights/overview", {"from": "not-a-date"}),
        ("/api/insights/overview", {"from": "2026-08-25T00:00:00"}),
        ("/api/insights/export", {"format": "xml"}),
    ],
)
async def test_invalid_ranges_and_formats_return_400(
    as_admin: AsyncClient,
    path: str,
    params: dict[str, str],
) -> None:
    response = await as_admin.get(path, params=params)
    assert response.status_code == 400
