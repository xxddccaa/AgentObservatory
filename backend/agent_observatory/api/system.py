"""Administrator-only local runtime status and redacted log access."""
from __future__ import annotations

import base64
import json
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from agent_observatory.api.deps import AdminPrincipal
from agent_observatory.api.health import readiness_payload
from agent_observatory.config import settings
from agent_observatory.services.redaction import redact_secrets
from agent_observatory.supervisor import Supervisor

router = APIRouter(prefix="/api/system", tags=["system"])
LOG_COMPONENTS = {"all", "api", "worker", "frontend", "supervisor"}


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        return max(0, int(base64.urlsafe_b64decode(padded).decode()))
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="invalid log cursor") from None


def _timestamp(value: object) -> str | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC).isoformat()
    return value if isinstance(value, str) else None


@router.get("/status")
async def system_status(principal: AdminPrincipal) -> dict[str, Any]:
    readiness = await readiness_payload()
    supervisor = Supervisor(settings().home).status()
    component_states = dict(readiness["components"])
    recovery_actions = list(readiness["recovery_actions"])
    runtime_degraded = False
    for name, runtime in supervisor.get("components", {}).items():
        component = component_states.setdefault(name, {})
        runtime_status = runtime.get("status", "unknown")
        component.update(
            {
                "pid": runtime.get("pid"),
                "restart_count": runtime.get("restart_count", 0),
                "runtime_status": runtime_status,
                "last_error": redact_secrets(runtime.get("last_error")),
                "next_restart_at": _timestamp(runtime.get("next_restart_at")),
            }
        )
        if runtime_status not in {"running"}:
            runtime_degraded = True
            recovery_actions.append(
                f"Inspect `{name}` logs with `agent-observatory logs {name}`."
            )
    supervisor_pid = supervisor.get("supervisor_pid")
    if not supervisor_pid:
        runtime_degraded = True
        recovery_actions.append("Start the application with `agent-observatory up`.")
    recovery_actions = list(dict.fromkeys(recovery_actions))
    return {
        "status": (
            "degraded"
            if runtime_degraded or readiness["status"] != "ok"
            else "ok"
        ),
        "environment": settings().environment,
        "generated_at": datetime.now(UTC).isoformat(),
        "supervisor": {
            "status": "running" if supervisor_pid else "offline",
            "pid": supervisor_pid,
            "updated_at": supervisor.get("updated_at"),
        },
        "components": component_states,
        "recovery_actions": recovery_actions,
        "recommendations": recovery_actions,
    }


def _log_paths(component: str) -> list[Path]:
    root = Path(settings().home).expanduser() / "logs"
    names = sorted(
        {path.name.split(".jsonl", 1)[0] for path in root.glob("*.jsonl*")}
    )
    if component != "all":
        names = [component]
    paths: list[Path] = []
    for name in names:
        current = root / f"{name}.jsonl"
        paths.extend(
            path
            for path in (
                *(
                    current.with_suffix(current.suffix + f".{index}")
                    for index in range(5, 0, -1)
                ),
                current,
            )
            if path.is_file()
        )
    return paths


def _read_recent(
    component: str, *, offset: int, limit: int
) -> tuple[list[dict[str, Any]], bool]:
    records: list[dict[str, Any]] = []
    for path in _log_paths(component):
        recent: deque[dict[str, Any]] = deque(maxlen=offset + limit + 1)
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        row = {
                            "ts": datetime.now(UTC).isoformat(),
                            "component": path.stem,
                            "stream": "stderr",
                            "message": line.rstrip(),
                        }
                    recent.append(row)
        except FileNotFoundError:
            continue
        records.extend(recent)
    newest_first = sorted(
        records,
        key=lambda row: str(row.get("ts") or row.get("timestamp") or ""),
        reverse=True,
    )
    page = newest_first[offset : offset + limit]
    has_more = len(newest_first) > offset + limit
    items = [
        {
            "timestamp": str(row.get("ts") or row.get("timestamp") or ""),
            "component": str(row.get("component") or "unknown"),
            "level": (
                "error"
                if row.get("stream") == "stderr"
                else str(row.get("level") or "info")
            ),
            "message": redact_secrets(str(row.get("message") or "")),
            **(
                {"request_id": row.get("request_id")}
                if row.get("request_id")
                else {}
            ),
        }
        for row in page
    ]
    return items, has_more


@router.get("/logs")
async def system_logs(
    principal: AdminPrincipal,
    component: str = Query(default="all"),
    cursor: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    if component not in LOG_COMPONENTS:
        raise HTTPException(status_code=400, detail="invalid log component")
    offset = _decode_cursor(cursor)
    items, has_more = _read_recent(component, offset=offset, limit=limit)
    return {
        "items": items,
        "next_cursor": _encode_cursor(offset + len(items)) if has_more else None,
    }


__all__ = ["router"]
