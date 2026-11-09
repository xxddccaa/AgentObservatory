"""Autonomous discovery of local agent session-data roots.

Read-only. Probes well-known home layouts for each supported agent so the
indexer can enumerate which agents have data on this machine without per-agent
configuration. Extra, non-standard roots may be supplied via
``AGENT_OBS_EXTRA_ROOTS`` (parsed by :mod:`agent_observatory.config`).

This module has no database or settings dependency: callers pass any extra
roots in, keeping it pure and unit-testable.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: agent -> well-known roots that may hold its session data (``~`` is expanded).
AGENT_LAYOUTS: dict[str, list[str]] = {
    "codex": ["~/.codex/sessions"],
    "stepcode": ["~/.stepcode/codex", "~/.stepcode/claude/projects"],
    "stepcli": ["~/.step-cli/sessions"],
    "claude": ["~/.claude/projects"],
    "dsh": ["~/.dsh/sessions"],
    "opencode": ["~/.local/share/opencode", "~/.config/opencode"],
    "pi": ["~/.pi/agent/sessions", "~/.stepgo/agents/pi/sessions"],
    "xdcode": ["~/.xdcode/sessions", "~/.xdcode/archived_sessions"],
    # Only the host's own run logs.  ``~/.stepgo/agents/*`` is intentionally
    # absent: those paths are normally symlinks to the native agent roots and
    # labelling the files as StepGo sessions would misclassify and double-index.
    "stepgo": ["~/.stepgo/sessions/host"],
}

#: Agents with a registered ingest adapter. ``generic`` is intentionally not
#: listed because it is a fallback for explicitly configured unknown roots.
IMPLEMENTED_AGENTS: frozenset[str] = frozenset(
    {
        "codex",
        "stepcode",
        "stepcli",
        "claude",
        "dsh",
        "opencode",
        "pi",
        "xdcode",
        "stepgo",
    }
)


@dataclass(frozen=True, slots=True)
class DetectedRoot:
    agent: str
    path: Path
    exists: bool
    implemented: bool


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(path))


def probe_agents(
    extra_roots: dict[str, list[str]] | None = None,
    *,
    layouts: dict[str, list[str]] | None = None,
) -> list[DetectedRoot]:
    """Return one :class:`DetectedRoot` per (agent, known-root), existence marked."""
    merged: dict[str, list[str]] = {
        agent: list(roots)
        for agent, roots in (layouts or AGENT_LAYOUTS).items()
    }
    for agent, roots in (extra_roots or {}).items():
        merged.setdefault(agent, [])
        merged[agent].extend(roots)
    detected: list[DetectedRoot] = []
    seen: set[tuple[str, str]] = set()
    for agent, roots in merged.items():
        for root in roots:
            path = _expand(root)
            try:
                real = str(path.resolve(strict=False))
            except OSError:
                real = str(path)
            key = (agent, real)
            if key in seen:
                continue
            seen.add(key)
            detected.append(
                DetectedRoot(
                    agent=agent,
                    path=path,
                    exists=path.exists(),
                    implemented=agent in IMPLEMENTED_AGENTS,
                )
            )
    return detected


def detected_agents(
    extra_roots: dict[str, list[str]] | None = None,
    *,
    layouts: dict[str, list[str]] | None = None,
) -> list[str]:
    """Sorted agent names that have at least one existing root on this machine."""
    return sorted(
        {d.agent for d in probe_agents(extra_roots, layouts=layouts) if d.exists}
    )


def summary(
    extra_roots: dict[str, list[str]] | None = None,
    *,
    layouts: dict[str, list[str]] | None = None,
) -> dict[str, dict]:
    """agent -> {exists, implemented, roots:[{path, exists}]} for health/heartbeat."""
    out: dict[str, dict] = {}
    for d in probe_agents(extra_roots, layouts=layouts):
        entry = out.setdefault(
            d.agent, {"exists": False, "implemented": d.implemented, "roots": []}
        )
        entry["roots"].append({"path": str(d.path), "exists": d.exists})
        entry["exists"] = entry["exists"] or d.exists
    return out


__all__ = [
    "AGENT_LAYOUTS",
    "IMPLEMENTED_AGENTS",
    "DetectedRoot",
    "probe_agents",
    "detected_agents",
    "summary",
]
