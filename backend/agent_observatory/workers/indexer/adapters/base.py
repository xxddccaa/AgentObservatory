"""Read-only source adapter protocol and plan data structures."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from agent_observatory.db.models import IndexCheckpoint


class PlanAction(StrEnum):
    UNCHANGED = "unchanged"
    APPEND = "append"
    REBUILD = "rebuild"


@dataclass(frozen=True, slots=True)
class SourceDescriptor:
    kind: str
    identity: str
    path: Path
    format: str
    agent: str = "codex"
    compression: str | None = None
    thread_id: str | None = None


@dataclass(frozen=True, slots=True)
class SourcePlan:
    source: SourceDescriptor
    action: PlanAction
    fingerprint: dict[str, Any]


class SourceAdapter(Protocol):
    kind: str
    agent: str
    parser_version: str

    def discover(self) -> list[SourceDescriptor]: ...

    def plan(
        self, source: SourceDescriptor, checkpoint: IndexCheckpoint | None
    ) -> SourcePlan: ...

    async def ingest(self, db: Any, plan: SourcePlan) -> Any: ...


__all__ = [
    "PlanAction",
    "SourceAdapter",
    "SourceDescriptor",
    "SourcePlan",
]
