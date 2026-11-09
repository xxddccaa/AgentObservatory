"""Public liveness and readiness probes."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent_observatory.config import settings
from agent_observatory.db.models import WorkerHeartbeat
from agent_observatory.db.session import get_engine, ping_database

router = APIRouter()
WORKER_TYPES = ("indexer",)


def _component(
    status: str,
    *,
    message: str | None = None,
    last_seen_at: datetime | None = None,
    last_success_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "healthy": status == "up",
        **({"message": message, "detail": message} if message else {}),
        **(
            {
                "last_seen_at": last_seen_at.isoformat(),
                "last_heartbeat_at": last_seen_at.isoformat(),
            }
            if last_seen_at
            else {}
        ),
        **(
            {"last_success_at": last_success_at.isoformat()}
            if last_success_at
            else {}
        ),
    }


@router.get("/health/live")
async def live(response: Response) -> dict[str, str]:
    """Process-only liveness probe; intentionally avoids all dependencies."""
    response.headers["Cache-Control"] = "no-store"
    return {"status": "ok", "version": settings().version}


async def readiness_payload() -> dict[str, Any]:
    components: dict[str, dict[str, Any]] = {}
    recovery: list[str] = []

    try:
        await ping_database()
    except Exception:  # noqa: BLE001 - public health must not leak internals
        components["postgresql"] = _component(
            "down", message="PostgreSQL is unreachable."
        )
        recovery.append("Start PostgreSQL and verify AGENT_OBS_DATABASE_URL.")
        pg = False
    else:
        components["postgresql"] = _component("up")
        pg = True

    worker_states = {kind: False for kind in WORKER_TYPES}
    if pg:
        try:
            factory = async_sessionmaker(get_engine(), expire_on_commit=False)
            async with factory() as db:
                rows = list((await db.scalars(select(WorkerHeartbeat))).all())
            fresh_after = datetime.now(UTC) - timedelta(
                seconds=max(settings().worker_heartbeat_interval * 3, 30)
            )
            for kind in WORKER_TYPES:
                candidates = [row for row in rows if row.worker_type == kind]
                latest = max(candidates, key=lambda row: row.heartbeat_at, default=None)
                if latest is None:
                    components[kind] = _component(
                        "down", message=f"{kind} worker has not reported a heartbeat."
                    )
                    recovery.append(f"Start or restart the {kind} worker.")
                elif latest.heartbeat_at < fresh_after:
                    components[kind] = _component(
                        "stale",
                        message=f"{kind} worker heartbeat is stale.",
                        last_seen_at=latest.heartbeat_at,
                        last_success_at=latest.last_success_at,
                    )
                    recovery.append(f"Inspect {kind} logs and restart the worker.")
                elif latest.status != "running" or latest.pid <= 0:
                    components[kind] = _component(
                        "degraded",
                        message=f"{kind} worker is degraded. Inspect local logs.",
                        last_seen_at=latest.heartbeat_at,
                        last_success_at=latest.last_success_at,
                    )
                    recovery.append(f"Inspect {kind} logs.")
                else:
                    components[kind] = _component(
                        "up",
                        last_seen_at=latest.heartbeat_at,
                        last_success_at=latest.last_success_at,
                    )
                    components[kind]["pid"] = latest.pid
                    worker_states[kind] = True
                if latest is not None:
                    # Per-cycle counters/timings published by the worker: the
                    # first place to look when polling looks slow.
                    cycle = (latest.metadata_json or {}).get("last_cycle")
                    if isinstance(cycle, dict):
                        components[kind]["last_cycle"] = cycle
                    local_scan = (latest.metadata_json or {}).get("local_scan")
                    if isinstance(local_scan, dict):
                        public_scan = dict(local_scan)
                        if public_scan.get("error"):
                            public_scan["error"] = "Local source scan failed. Inspect local logs."
                        components[kind]["local_scan"] = public_scan
        except Exception:  # noqa: BLE001
            for kind in WORKER_TYPES:
                components[kind] = _component(
                    "degraded", message="Worker status could not be read."
                )
            recovery.append("Run `agent-observatory doctor`.")
    else:
        for kind in WORKER_TYPES:
            components[kind] = _component(
                "degraded",
                message="Worker state is unavailable while PostgreSQL is down.",
            )

    healthy = all(item["status"] == "up" for item in components.values())
    return {
        "status": "ok" if healthy else "degraded",
        "version": settings().version,
        "environment": settings().environment,
        "postgresql": pg,
        "workers": worker_states,
        "components": components,
        "recovery_actions": recovery,
    }


@router.get("/health/ready")
async def ready(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await readiness_payload()


@router.get("/health")
async def health(response: Response) -> dict[str, Any]:
    """Legacy path kept as the readiness endpoint for existing probes."""
    response.headers["Cache-Control"] = "no-store"
    return await readiness_payload()


__all__ = ["router", "readiness_payload"]
