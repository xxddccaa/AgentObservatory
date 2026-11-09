from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from agent_observatory.db.models import StepGoRun, StepGoRunEvent
from agent_observatory.workers.indexer.adapters import PlanAction
from agent_observatory.workers.indexer.pi import PiJsonlSource
from agent_observatory.workers.indexer.stepgo_host import (
    StepGoHostSource,
    extract_link_facts,
)


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def _append(path: Path, rows: list[dict]) -> None:
    with path.open("a") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _pi_session(path: Path, *, thread_id: str) -> None:
    _write(
        path,
        [
            {
                "type": "session",
                "version": 3,
                "id": thread_id,
                "timestamp": "2026-08-21T10:00:00Z",
                "cwd": "/tmp/pi-linked",
            }
        ],
    )


def test_extract_link_facts_ignores_host_session_id() -> None:
    ids, paths = extract_link_facts(
        {
            "type": "session.start",
            "sessionId": "host-run-only",
            "data": {"nativeSessionId": "native-1"},
            "chunk": json.dumps({"transcriptPath": "/tmp/native.jsonl"}),
        }
    )
    assert ids == {"native-1"}
    assert paths == {"/tmp/native.jsonl"}


@pytest.mark.anyio
async def test_host_run_keeps_raw_events_and_terminal_status(
    tmp_path: Path, db_session
) -> None:
    root = tmp_path / "host"
    path = root / "run-complete.jsonl"
    _write(
        path,
        [
            {
                "ts": "2026-08-21T10:00:00Z",
                "type": "session.start",
                "sessionId": "run-complete",
                "agent": "pi",
                "module": "agent",
            },
            {
                "ts": "2026-08-21T10:00:01Z",
                "type": "stream",
                "sessionId": "run-complete",
                "side": "stdout",
                "chunk": "hello",
            },
            {
                "ts": "2026-08-21T10:00:02Z",
                "type": "future_host_event",
                "sessionId": "run-complete",
                "payload": {"opaque": True},
            },
            {
                "ts": "2026-08-21T10:00:03Z",
                "type": "session.end",
                "sessionId": "run-complete",
                "data": {"exitCode": 3, "metrics": {"durationMs": 3000}},
            },
        ],
    )
    adapter = StepGoHostSource(root)
    source = adapter.discover()[0]
    run = await adapter.ingest(db_session, adapter.plan(source, None))

    assert run.status == "failed"
    assert run.exit_code == 3
    assert run.metrics_json == {"durationMs": 3000}
    assert run.event_count == 4
    assert run.link_status == "unlinked"
    assert run.link_reason == "no_native_identity"
    assert run.raw_start_json["agent"] == "pi"
    assert run.raw_end_json["data"]["exitCode"] == 3

    events = list(
        (
            await db_session.scalars(
                select(StepGoRunEvent)
                .where(StepGoRunEvent.run_id == run.id)
                .order_by(StepGoRunEvent.event_index)
            )
        ).all()
    )
    unknown = next(event for event in events if event.event_type == "future_host_event")
    assert unknown.is_known is False
    assert unknown.raw_json["payload"]["opaque"] is True

    assert (
        adapter.plan_for_run(source, run).action == PlanAction.UNCHANGED
    )

    # Host output that arrives after session.end is still evidence, but it must
    # not rewrite the terminal state or the recorded end time.
    original_end = run.end_time
    _append(
        path,
        [
            {
                "ts": "2026-08-21T10:00:04Z",
                "type": "stream",
                "sessionId": "run-complete",
                "side": "stderr",
                "chunk": "late output",
            }
        ],
    )
    assert (
        adapter.plan_for_run(source, run).action == PlanAction.APPEND
    )
    run = await adapter.ingest(db_session, adapter.plan_for_run(source, run))
    assert run.event_count == 5
    assert run.status == "failed"
    assert run.end_time == original_end


@pytest.mark.anyio
async def test_host_session_id_is_not_used_as_native_identity(
    tmp_path: Path, db_session
) -> None:
    pi_root = tmp_path / "pi"
    pi_path = pi_root / "session.jsonl"
    _pi_session(pi_path, thread_id="host-run-only")
    pi_adapter = PiJsonlSource(pi_root)
    pi_source = pi_adapter.discover()[0]
    native = await pi_adapter.ingest(db_session, pi_adapter.plan(pi_source, None))

    host_root = tmp_path / "host"
    host_path = host_root / "host-run-only.jsonl"
    _write(
        host_path,
        [
            {
                "ts": "2026-08-21T10:00:00Z",
                "type": "session.start",
                "sessionId": "host-run-only",
                "agent": "pi",
            },
            {
                "ts": "2026-08-21T10:00:01Z",
                "type": "session.end",
                "sessionId": "host-run-only",
                "data": {"exitCode": 0},
            },
        ],
    )
    adapter = StepGoHostSource(host_root)
    source = adapter.discover()[0]
    run = await adapter.ingest(db_session, adapter.plan(source, None))

    assert native.thread_id == "host-run-only"
    assert run.native_session_id is None
    assert run.link_status == "unlinked"
    assert run.link_reason == "no_native_identity"
    assert await adapter.relink_pending(db_session) == 0
    assert run.linked_agent_session_id is None


@pytest.mark.anyio
async def test_explicit_native_id_relinks_after_late_native_index(
    tmp_path: Path, db_session
) -> None:
    host_root = tmp_path / "host"
    host_path = host_root / "run-linked.jsonl"
    _write(
        host_path,
        [
            {
                "ts": "2026-08-21T10:00:00Z",
                "type": "session.start",
                "sessionId": "run-linked",
                "agent": "pi",
                "nativeSessionId": "pi-thread-linked",
            }
        ],
    )
    host = StepGoHostSource(host_root)
    host_source = host.discover()[0]
    run = await host.ingest(db_session, host.plan(host_source, None))
    assert run.link_status == "unlinked"
    assert run.link_reason == "native_session_not_indexed"

    pi_root = tmp_path / "pi"
    pi_path = pi_root / "session.jsonl"
    _pi_session(pi_path, thread_id="pi-thread-linked")
    pi = PiJsonlSource(pi_root)
    pi_source = pi.discover()[0]
    native = await pi.ingest(db_session, pi.plan(pi_source, None))

    assert await host.relink_pending(db_session) == 1
    assert run.link_status == "linked"
    assert run.linked_agent_session_id == native.id
    assert run.link_reason == "native_session_id"

    _append(
        host_path,
        [
            {
                "ts": "2026-08-21T10:00:02Z",
                "type": "session.end",
                "sessionId": "run-linked",
                "data": {"exitCode": 0},
            }
        ],
    )
    run = await host.ingest(db_session, host.plan_for_run(host_source, run))
    assert run.status == "completed"
    assert run.linked_agent_session_id == native.id


@pytest.mark.anyio
async def test_explicit_transcript_path_links_native_session(
    tmp_path: Path, db_session
) -> None:
    pi_root = tmp_path / "pi"
    pi_path = pi_root / "session.jsonl"
    _pi_session(pi_path, thread_id="pi-path-thread")
    pi = PiJsonlSource(pi_root)
    pi_source = pi.discover()[0]
    native = await pi.ingest(db_session, pi.plan(pi_source, None))

    host_root = tmp_path / "host"
    host_path = host_root / "run-path.jsonl"
    _write(
        host_path,
        [
            {
                "ts": "2026-08-21T10:00:00Z",
                "type": "session.start",
                "sessionId": "run-path",
                "agent": "pi",
                "transcriptPath": str(pi_path),
            },
            {
                "ts": "2026-08-21T10:00:01Z",
                "type": "session.end",
                "sessionId": "run-path",
                "data": {"exitCode": 0},
            },
        ],
    )
    host = StepGoHostSource(host_root)
    source = host.discover()[0]
    run = await host.ingest(db_session, host.plan(source, None))

    assert run.link_status == "linked"
    assert run.linked_agent_session_id == native.id
    assert run.link_reason == "transcript_path"
    assert run.native_session_id is None
    assert run.transcript_path == str(pi_path)


@pytest.mark.anyio
async def test_missing_native_identity_does_not_guess_from_time(
    tmp_path: Path, db_session
) -> None:
    pi_root = tmp_path / "pi"
    pi_path = pi_root / "session.jsonl"
    _pi_session(pi_path, thread_id="some-native-thread")
    pi = PiJsonlSource(pi_root)
    pi_source = pi.discover()[0]
    await pi.ingest(db_session, pi.plan(pi_source, None))

    host_root = tmp_path / "host"
    host_path = host_root / "run-untimed.jsonl"
    _write(
        host_path,
        [
            {
                "ts": "2026-08-21T10:00:00Z",
                "type": "session.start",
                "sessionId": "run-untimed",
                "agent": "pi",
            },
            {
                "ts": "2026-08-21T10:00:01Z",
                "type": "session.end",
                "sessionId": "run-untimed",
                "data": {"exitCode": 0},
            },
        ],
    )
    host = StepGoHostSource(host_root)
    source = host.discover()[0]
    run = await host.ingest(db_session, host.plan(source, None))
    assert run.link_status == "unlinked"
    assert run.linked_agent_session_id is None

    runs = list((await db_session.scalars(select(StepGoRun))).all())
    assert len(runs) == 1


@pytest.mark.anyio
async def test_ambiguous_native_id_is_not_linked(tmp_path: Path, db_session) -> None:
    from agent_observatory.db.models import AgentSession

    for suffix in ("one", "two"):
        db_session.add(
            AgentSession(
                id=f"native-{suffix}",
                thread_id="duplicate-thread",
                agent="pi",
                source_identity=f"pi:{suffix}",
                source_path=f"/tmp/{suffix}.jsonl",
                rollout_path=f"/tmp/{suffix}.jsonl",
                rollout_real_path=f"/tmp/{suffix}.jsonl",
            )
        )
    await db_session.flush()

    host_root = tmp_path / "host"
    host_path = host_root / "run-ambiguous.jsonl"
    _write(
        host_path,
        [
            {
                "ts": "2026-08-21T10:00:00Z",
                "type": "session.start",
                "sessionId": "run-ambiguous",
                "agent": "pi",
                "nativeSessionId": "duplicate-thread",
            }
        ],
    )
    host = StepGoHostSource(host_root)
    source = host.discover()[0]
    run = await host.ingest(db_session, host.plan(source, None))

    assert run.link_status == "ambiguous"
    assert run.linked_agent_session_id is None
    assert run.link_reason == "multiple_native_sessions"


@pytest.mark.anyio
async def test_stepgo_api_returns_linked_and_unlinked_runs_without_duplicate_sessions(
    tmp_path: Path,
    db_session,
    client: AsyncClient,
    as_operator,
) -> None:
    pi_root = tmp_path / "pi"
    pi_path = pi_root / "session.jsonl"
    _pi_session(pi_path, thread_id="api-pi-thread")
    pi = PiJsonlSource(pi_root)
    pi_source = pi.discover()[0]
    native = await pi.ingest(db_session, pi.plan(pi_source, None))

    host_root = tmp_path / "host"
    linked_path = host_root / "run-api-linked.jsonl"
    _write(
        linked_path,
        [
            {
                "ts": "2026-08-21T10:00:00Z",
                "type": "session.start",
                "sessionId": "run-api-linked",
                "agent": "pi",
                "nativeSessionId": "api-pi-thread",
            },
            {
                "ts": "2026-08-21T10:00:01Z",
                "type": "stream",
                "sessionId": "run-api-linked",
                "side": "stderr",
                "chunk": "Bearer abcdefghijklmnopqrstuvwxyz123456",
            },
            {
                "ts": "2026-08-21T10:00:02Z",
                "type": "session.end",
                "sessionId": "run-api-linked",
                "data": {"exitCode": 0},
            },
        ],
    )
    unlinked_path = host_root / "run-api-unlinked.jsonl"
    _write(
        unlinked_path,
        [
            {
                "ts": "2026-08-21T11:00:00Z",
                "type": "session.start",
                "sessionId": "run-api-unlinked",
                "agent": "pi",
            },
            {
                "ts": "2026-08-21T11:00:01Z",
                "type": "session.end",
                "sessionId": "run-api-unlinked",
                "data": {"exitCode": 1},
            },
        ],
    )
    host = StepGoHostSource(host_root)
    for source in host.discover():
        await host.ingest(db_session, host.plan(source, None))
    await db_session.flush()

    listing = (await client.get("/api/codex/sessions")).json()["sessions"]
    assert len(listing) == 1
    row = listing[0]
    assert row["agent"] == "pi"
    assert row["launch_sources"] == ["stepgo"]
    assert row["stepgo_run_count"] == 1

    pi_filtered = (
        await client.get("/api/codex/sessions", params={"agent": "pi"})
    ).json()["sessions"]
    stepgo_filtered = (
        await client.get("/api/codex/sessions", params={"agent": "stepgo"})
    ).json()["sessions"]
    assert [item["id"] for item in pi_filtered] == [native.id]
    assert [item["id"] for item in stepgo_filtered] == [native.id]

    detail = (await client.get(f"/api/codex/sessions/{native.id}")).json()
    assert detail["launch_sources"] == ["stepgo"]
    assert len(detail["stepgo_runs"]) == 1
    linked_run = detail["stepgo_runs"][0]
    assert linked_run["linked_agent_session_id"] == native.id
    assert linked_run["status"] == "completed"
    assert linked_run["start_time"] == "2026-08-21T10:00:00+00:00"
    assert linked_run["end_time"] == "2026-08-21T10:00:02+00:00"

    runs = (await client.get("/api/codex/stepgo/runs")).json()["runs"]
    assert len(runs) == 2
    assert {run["link_status"] for run in runs} == {"linked", "unlinked"}
    unlinked = [
        run
        for run in runs
        if run["host_run_id"] == "run-api-unlinked"
    ][0]
    assert unlinked["exit_code"] == 1
    assert unlinked["status"] == "failed"
    assert unlinked["session"] is None

    unlinked_only = (
        await client.get(
            "/api/codex/stepgo/runs", params={"link_status": "unlinked"}
        )
    ).json()["runs"]
    assert [run["host_run_id"] for run in unlinked_only] == ["run-api-unlinked"]

    events = (
        await client.get(f"/api/codex/stepgo/runs/{linked_run['id']}/events")
    ).json()["events"]
    stream = next(event for event in events if event["event_type"] == "stream")
    assert stream["raw_json"]["chunk"] == "Bearer [REDACTED_SECRET]"
    first_page = (await client.get("/api/codex/stepgo/runs", params={"limit": 1})).json()
    assert first_page["next_cursor"]
    second_page = (await client.get("/api/codex/stepgo/runs", params={
        "limit": 1, "cursor": first_page["next_cursor"],
    })).json()
    assert second_page["next_cursor"] is None
    assert first_page["runs"][0]["id"] != second_page["runs"][0]["id"]
    event_page = (await client.get(f"/api/codex/stepgo/runs/{linked_run['id']}/events", params={"limit": 1})).json()
    next_events = (await client.get(f"/api/codex/stepgo/runs/{linked_run['id']}/events", params={
        "limit": 2, "after": event_page["next_cursor"],
    })).json()
    assert [event["event_index"] for event in next_events["events"]] == [1, 2]
    assert next_events["next_cursor"] is None


@pytest.mark.anyio
async def test_conflicting_explicit_ids_stay_ambiguous(tmp_path: Path, db_session):
    native_path = tmp_path / "pi" / "session.jsonl"
    _pi_session(native_path, thread_id="valid-native")
    pi = PiJsonlSource(native_path.parent)
    native_source = pi.discover()[0]
    await pi.ingest(db_session, pi.plan(native_source, None))
    host_path = tmp_path / "host" / "run.jsonl"
    _write(host_path, [
        {"type": "session.start", "ts": "2026-08-21T10:00:00Z", "agent": "pi",
         "nativeSessionId": "valid-native"},
        {"type": "status", "nativeSessionId": "other-native"},
    ])
    host = StepGoHostSource(host_path.parent)
    source = host.discover()[0]
    run = await host.ingest(db_session, host.plan(source, None))
    assert run.link_status == "ambiguous"
    assert run.linked_agent_session_id is None
    assert run.link_reason == "conflicting_native_identity"
    assert run.status == "unknown"
    assert run.end_time is None
    await host.relink_pending(db_session)
    assert run.link_status == "ambiguous"


@pytest.mark.anyio
async def test_host_growing_rewrite_rebuilds_without_rehashing_whole_file(tmp_path: Path, db_session):
    path = tmp_path / "run.jsonl"
    _write(path, [{"type": "session.start", "agent": "pi"}])
    host = StepGoHostSource(tmp_path)
    source = host.discover()[0]
    run = await host.ingest(db_session, host.plan(source, None))
    _write(path, [
        {"type": "session.start", "agent": "codex", "ts": "2026-08-22T10:00:00Z"},
        {"type": "session.end", "ts": "2026-08-22T10:00:01Z", "data": {"exitCode": 0}},
    ])
    plan = host.plan_for_run(source, run)
    assert plan.action.value == "rebuild"
    await host.ingest(db_session, plan)
    assert run.agent == "codex"
    assert run.event_count == 2
    assert run.start_time.isoformat() == "2026-08-22T10:00:00+00:00"
    assert run.file_sha256 is None
    assert run.fingerprint_json["head_length"] <= 4096
