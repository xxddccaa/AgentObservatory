from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from agent_observatory.api.projects import router as projects_router
from agent_observatory.db.models import AgentSession, AgentTurn, Workspace
from agent_observatory.services import projects

NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _mount_projects(app) -> None:
    if not any(
        getattr(route, "path", "").startswith("/api/projects") for route in app.routes
    ):
        before = len(app.routes)
        app.include_router(projects_router)
        added = app.router.routes[before:]
        del app.router.routes[before:]
        fallback_index = next(
            index
            for index, route in enumerate(app.router.routes)
            if getattr(route, "path", "") == "/{path:path}"
        )
        app.router.routes[fallback_index:fallback_index] = added


def _workspace(workspace_id: str, path: str) -> Workspace:
    return Workspace(
        id=workspace_id,
        path=path,
        normalized_path=path,
        display_name=path.rstrip("/").rsplit("/", 1)[-1],
        is_allowed=True,
    )


def _session(
    session_id: str,
    workspace_id: str | None,
    *,
    at: datetime,
    agent: str = "codex",
    model: str = "gpt-5",
    is_subagent: bool = False,
) -> AgentSession:
    return AgentSession(
        id=session_id,
        thread_id=f"thread-{session_id}",
        rollout_path=f"/tmp/{session_id}.jsonl",
        rollout_real_path=f"/real/{session_id}.jsonl",
        source_identity=f"test:{session_id}",
        source_path=f"/real/{session_id}.jsonl",
        agent=agent,
        model=model,
        cwd=f"/workspace/{workspace_id}" if workspace_id else None,
        workspace_id=workspace_id,
        start_time=at - timedelta(minutes=2),
        end_time=at,
        updated_at=at,
        is_subagent=is_subagent,
    )


def _turn(
    turn_id: str,
    session_id: str,
    *,
    index: int,
    at: datetime,
    total: int,
    agent: str = "codex",
) -> AgentTurn:
    return AgentTurn(
        id=turn_id,
        agent=agent,
        agent_session_id=session_id,
        turn_index=index,
        started_at=at,
        completed_at=at + timedelta(seconds=1),
        input_tokens=total // 2,
        cached_input_tokens=total // 10,
        output_tokens=total // 2,
        reasoning_output_tokens=total // 20,
        total_tokens=total,
        status="completed",
    )


def test_resolve_window_uses_utc_half_open_boundaries():
    since, until, name = projects.resolve_window("day", now=NOW)
    assert name == "day"
    assert since == NOW - timedelta(days=1)
    assert until == NOW
    assert projects.resolve_window("all", now=NOW)[0] is None
    with pytest.raises(ValueError):
        projects.resolve_window("year", now=NOW)


@pytest.mark.asyncio
async def test_project_grouping_includes_subagent_and_exact_workspace(
    db_session: AsyncSession,
):
    db_session.add_all(
        [
            _workspace("workspace-a", "/repo/project"),
            _workspace("workspace-b", "/repo/project/sub"),
        ]
    )
    await db_session.flush()
    db_session.add_all(
        [
            _session("s-parent", "workspace-a", at=NOW - timedelta(hours=1)),
            _session(
                "s-child",
                "workspace-a",
                at=NOW - timedelta(minutes=30),
                agent="stepcode",
                is_subagent=True,
            ),
            _session("s-subdir", "workspace-b", at=NOW - timedelta(minutes=10)),
        ]
    )
    await db_session.flush()
    db_session.add_all(
        [
            _turn("t-parent", "s-parent", index=0, at=NOW - timedelta(hours=1), total=100),
            _turn(
                "t-child",
                "s-child",
                index=0,
                at=NOW - timedelta(minutes=30),
                total=200,
                agent="stepcode",
            ),
            _turn(
                "t-subdir",
                "s-subdir",
                index=0,
                at=NOW - timedelta(minutes=10),
                total=300,
            ),
        ]
    )
    result = await projects.list_projects(db_session, now=NOW)
    assert [item["id"] for item in result["items"]] == ["workspace-b", "workspace-a"]
    project_a = next(item for item in result["items"] if item["id"] == "workspace-a")
    assert project_a["session_count"] == 2
    assert project_a["turn_count"] == 2
    assert project_a["all_time"]["total_tokens"] == 300


@pytest.mark.asyncio
async def test_project_list_search_sort_and_agent_model_filters(
    db_session: AsyncSession,
):
    db_session.add_all(
        [
            _workspace("workspace-old", "/repo/OldProject"),
            _workspace("workspace-new", "/repo/NewProject"),
        ]
    )
    await db_session.flush()
    db_session.add_all(
        [
            _session(
                "s-old",
                "workspace-old",
                at=NOW - timedelta(days=3),
                agent="codex",
                model="gpt-4",
            ),
            _session(
                "s-new",
                "workspace-new",
                at=NOW - timedelta(hours=1),
                agent="stepcode",
                model="gpt-5",
            ),
        ]
    )
    await db_session.flush()
    db_session.add_all(
        [
            _turn("t-old", "s-old", index=0, at=NOW - timedelta(days=3), total=10),
            _turn(
                "t-new",
                "s-new",
                index=0,
                at=NOW - timedelta(hours=1),
                total=20,
                agent="stepcode",
            ),
        ]
    )
    result = await projects.list_projects(
        db_session, q="newproject", agent="stepcode", model="gpt-5", now=NOW
    )
    assert result["total"] == 1
    assert result["items"][0]["id"] == "workspace-new"
    assert result["items"][0]["selected_range"]["total_tokens"] == 20


@pytest.mark.asyncio
async def test_project_detail_and_timeseries_windows(db_session: AsyncSession):
    db_session.add(_workspace("workspace-a", "/repo/project"))
    await db_session.flush()
    session_one = _session("s1", "workspace-a", at=NOW - timedelta(hours=2))
    session_one.end_time = NOW - timedelta(minutes=30)
    session_one.updated_at = NOW - timedelta(minutes=20)
    db_session.add_all(
        [session_one, _session("s2", "workspace-a", at=NOW - timedelta(days=2), agent="stepcode")]
    )
    await db_session.flush()
    db_session.add_all(
        [
            _turn("t1", "s1", index=0, at=NOW - timedelta(hours=2), total=100),
            _turn("t2", "s2", index=0, at=NOW - timedelta(days=2), total=200, agent="stepcode"),
        ]
    )
    detail = await projects.get_project(db_session, "workspace-a", now=NOW)
    assert detail is not None
    assert detail["totals"]["all_time"]["total_tokens"] == 300
    assert detail["totals"]["day"]["total_tokens"] == 100
    assert detail["totals"]["week"]["total_tokens"] == 300
    assert {item["agent"] for item in detail["by_agent"]} == {"codex", "stepcode"}
    assert detail["project"]["last_active_at"] == "2026-08-31T11:30:00+00:00"
    assert detail["project"]["first_active_at"] == "2026-08-29T11:58:00+00:00"

    await db_session.execute(text("SET TIME ZONE 'Asia/Shanghai'"))
    series = await projects.project_timeseries(
        db_session, "workspace-a", range_name="day", now=NOW
    )
    assert series is not None
    assert series["bucket"] == "hour"
    assert sum(point["total_tokens"] for point in series["points"]) == 100
    assert series["points"][0]["timestamp"] == "2026-08-31T10:00:00+00:00"
    assert await projects.get_project(db_session, "missing", now=NOW) is None
    assert await projects.project_timeseries(
        db_session, "missing", range_name="day", now=NOW
    ) is None


@pytest.mark.asyncio
async def test_projects_api_and_history_workspace_filter(
    client, as_viewer, db_session: AsyncSession
):
    db_session.add_all(
        [
            _workspace("workspace-a", "/repo/project"),
            _workspace("workspace-b", "/repo/other"),
        ]
    )
    await db_session.flush()
    db_session.add_all(
        [
            _session("s-a", "workspace-a", at=NOW),
            _session("s-b", "workspace-b", at=NOW),
        ]
    )
    await db_session.flush()
    db_session.add(_turn("t-a", "s-a", index=0, at=NOW, total=42))

    response = await client.get("/api/projects", params={"range": "day"})
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 2
    detail = await client.get("/api/projects/workspace-a")
    assert detail.status_code == 200
    missing = await client.get("/api/projects/unknown")
    assert missing.status_code == 404

    sessions = await client.get(
        "/api/codex/sessions", params={"workspace_id": "workspace-a"}
    )
    assert sessions.status_code == 200
    assert [row["id"] for row in sessions.json()["sessions"]] == ["s-a"]
