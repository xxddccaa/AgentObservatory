"""Read-only source adapters and indexing plans."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from agent_observatory.db.models import IndexCheckpoint

from .fingerprints import file_fingerprint, indexed_content_matches
from .scanner import RolloutScanner
from .stepcode import StepCodeImporter


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


class CodexJsonlSource:
    kind = "codex_jsonl"
    agent = "codex"

    def __init__(
        self,
        scanner: RolloutScanner,
        *,
        agent: str | None = None,
        source_kind: str | None = None,
        identity_prefix: str | None = None,
        kind: str | None = None,
    ) -> None:
        self.scanner = scanner
        self.agent = agent or scanner.agent
        self.kind = kind or source_kind or scanner.source_kind
        self.identity_prefix = identity_prefix or scanner.identity_prefix

    @property
    def parser_version(self) -> str:
        return self.scanner.parser_version

    @staticmethod
    def identity(path: Path, prefix: str = "codex") -> str:
        return RolloutScanner.source_identity(path, prefix)

    def source_identity(self, path: Path) -> str:
        return self.identity(path, self.identity_prefix)

    def discover(self) -> list[SourceDescriptor]:
        return [
            SourceDescriptor(
                kind=self.kind,
                identity=self.source_identity(path),
                path=path,
                format="jsonl",
                agent=self.agent,
                compression="zstd" if path.name.endswith(".zst") else None,
            )
            for path in self.scanner.discover()
        ]

    def plan(
        self, source: SourceDescriptor, checkpoint: IndexCheckpoint | None
    ) -> SourcePlan:
        stat = source.path.stat()
        fingerprint: dict[str, Any] = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "path": str(source.path.resolve()),
            "compression": source.compression,
        }
        if checkpoint is None or checkpoint.parser_version != self.scanner.parser_version:
            return SourcePlan(source, PlanAction.REBUILD, {**fingerprint, **file_fingerprint(source.path)})
        previous = checkpoint.fingerprint_json or {}
        cursor = checkpoint.cursor_json or {}
        if previous.get("path") != fingerprint["path"]:
            return SourcePlan(source, PlanAction.REBUILD, {**fingerprint, **file_fingerprint(source.path)})
        if previous.get("compression") != source.compression:
            return SourcePlan(source, PlanAction.REBUILD, fingerprint)
        if source.compression == "zstd":
            if stat.st_size < int(previous.get("size", 0)):
                return SourcePlan(source, PlanAction.REBUILD, fingerprint)
        elif stat.st_size < int(cursor.get("byte_offset", 0)):
            return SourcePlan(source, PlanAction.REBUILD, fingerprint)
        if (
            stat.st_size == previous.get("size")
            and stat.st_mtime_ns == previous.get("mtime_ns")
            and checkpoint.status == "indexed"
        ):
            return SourcePlan(source, PlanAction.UNCHANGED, fingerprint)
        fingerprint.update(file_fingerprint(source.path))
        if not indexed_content_matches(source.path, previous):
            return SourcePlan(source, PlanAction.REBUILD, fingerprint)
        # The scanner performs the content-hash same-size rewrite check before
        # writing, while this cheap metadata plan avoids hashing every poll.
        action = PlanAction.REBUILD if stat.st_size == previous.get("size") else PlanAction.APPEND
        return SourcePlan(source, action, fingerprint)

    async def ingest(self, db: Any, plan: SourcePlan) -> Any:
        """Ingest through the canonical rollout scanner.

        The orchestrator still owns checkpoint bookkeeping, but exposing this
        method keeps every registered adapter conformant to ``SourceAdapter``.
        """
        session = await self.scanner.index_path(
            db, plan.source.path, force_rebuild=plan.action == PlanAction.REBUILD
        )
        session.agent = plan.source.agent
        return session


class XDCodeJsonlSource(CodexJsonlSource):
    """XDCode rollout adapter built on the Codex-compatible reader.

    The file format is Codex-compatible but XDCode owns a distinct agent,
    source kind and identity namespace.  Sharing the scanner keeps append,
    truncation and checkpoint semantics identical instead of forking them.
    """

    kind = "xdcode_jsonl"
    agent = "xdcode"

    def __init__(self, scanner: RolloutScanner) -> None:
        if (scanner.agent, scanner.source_kind, scanner.identity_prefix) != (
            "xdcode", "xdcode_jsonl", "xdcode"
        ):
            scanner = RolloutScanner(
                scanner.root,
                parser_version=scanner.parser_version,
                agent="xdcode",
                source_kind="xdcode_jsonl",
                identity_prefix="xdcode",
            )
        super().__init__(
            scanner,
            agent="xdcode",
            source_kind="xdcode_jsonl",
            identity_prefix="xdcode",
            kind="xdcode_jsonl",
        )


class StepCodeSqliteSource:
    kind = "stepcode_sqlite"
    agent = "stepcode"

    def __init__(
        self, importer: StepCodeImporter, *, parser_version: str = "4"
    ) -> None:
        self.importer = importer
        self.parser_version = parser_version

    def discover(self) -> list[SourceDescriptor]:
        sources: list[SourceDescriptor] = []
        for path in self.importer.discover():
            for thread_id in self.importer.iter_threads(path):
                sources.append(
                    SourceDescriptor(
                        kind=self.kind,
                        identity=self.importer.source_identity(path, thread_id),
                        path=path,
                        format="sqlite",
                        agent=self.agent,
                        thread_id=thread_id,
                    )
                )
        return sources

    def plan(
        self, source: SourceDescriptor, checkpoint: IndexCheckpoint | None
    ) -> SourcePlan:
        assert source.thread_id is not None
        state = self.importer.thread_state(source.path, source.thread_id)
        stat = source.path.stat()
        fingerprint: dict[str, Any] = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            **state,
        }
        if checkpoint is None:
            return SourcePlan(source, PlanAction.REBUILD, fingerprint)
        previous = checkpoint.fingerprint_json or {}
        cursor = checkpoint.cursor_json or {}
        if checkpoint.parser_version != self.parser_version:
            return SourcePlan(source, PlanAction.REBUILD, fingerprint)
        state_keys = (
            "item_count",
            "max_rollout_ordinal",
            "max_updated_at_ordinal",
        )
        if (
            all(fingerprint[key] == previous.get(key) for key in state_keys)
            and checkpoint.status == "indexed"
        ):
            return SourcePlan(source, PlanAction.UNCHANGED, fingerprint)
        max_rollout = int(cursor.get("rollout_ordinal", -1))
        max_updated = int(cursor.get("updated_at_ordinal", -1))
        if self.importer.has_updated_items(
            source.path,
            thread_id=source.thread_id,
            through_ordinal=max_rollout,
            after_updated_ordinal=max_updated,
        ):
            return SourcePlan(source, PlanAction.REBUILD, fingerprint)
        if int(state["max_rollout_ordinal"]) > max_rollout:
            return SourcePlan(source, PlanAction.APPEND, fingerprint)
        # Turn-only changes and deletion/replacement are safest as a rebuild.
        return SourcePlan(source, PlanAction.REBUILD, fingerprint)

    async def ingest(self, db: Any, plan: SourcePlan) -> Any:
        """Project one StepCode thread using the shared ingestion pipeline."""
        from .ingestion import EventIngestor

        scanner = RolloutScanner(self.importer.root)
        ingestor = EventIngestor(scanner, self.importer)
        return await ingestor.ingest_stepcode(db, plan)


__all__ = [
    "CodexJsonlSource",
    "PlanAction",
    "SourceDescriptor",
    "SourceAdapter",
    "SourcePlan",
    "StepCodeSqliteSource",
    "XDCodeJsonlSource",
]
