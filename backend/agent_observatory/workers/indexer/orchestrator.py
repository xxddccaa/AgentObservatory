"""Polling orchestration for independent, durable indexing sources."""
from __future__ import annotations

import asyncio
import hashlib
import os
import socket
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_observatory.config import settings
from agent_observatory.db.models import (
    AgentSession,
    CodexMessage,
    LocalDataSource,
    StepGoRun,
    WorkerHeartbeat,
    Workspace,
)
from agent_observatory.services import audit

from . import discovery
from .adapters import (
    CodexJsonlSource,
    PlanAction,
    SourceAdapter,
    SourceDescriptor,
    SourcePlan,
    StepCodeSqliteSource,
)
from .checkpoints import CheckpointRepository
from .ingestion import EventIngestor
from .linking import SessionLinker, has_visible_text
from .scanner import RolloutScanner
from .stepcode import StepCodeImporter
from .stepgo_host import StepGoHostSource


def _elapsed_ms(started: float) -> float:
    """Milliseconds since ``started`` (``time.perf_counter``), rounded to 0.1ms."""
    return round((time.perf_counter() - started) * 1000, 1)


@dataclass(slots=True)
class CycleStats:
    """Counters and timings for one polling cycle.

    Published through the worker heartbeat so a slow cycle can be diagnosed
    from the database instead of by sampling the process.
    """

    discovered: int = 0
    unchanged: int = 0
    changed: int = 0
    indexed: int = 0
    duplicates_skipped: int = 0
    failures: int = 0
    maintenance: dict[str, int] = field(default_factory=dict)
    finished: bool = False
    discover_ms: float = 0.0
    plan_ms: float = 0.0
    ingest_ms: float = 0.0
    maintain_ms: float = 0.0
    cycle_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "discovered": self.discovered,
            "unchanged": self.unchanged,
            "changed": self.changed,
            "indexed": self.indexed,
            "duplicates_skipped": self.duplicates_skipped,
            "failures": self.failures,
            "maintenance": dict(self.maintenance),
            "finished": self.finished,
            "ms": {
                "discover": self.discover_ms,
                "plan": self.plan_ms,
                "ingest": self.ingest_ms,
                "maintain": self.maintain_ms,
                "cycle": self.cycle_ms,
            },
        }


class IndexerOrchestrator:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        scanner: RolloutScanner,
        stepcode: StepCodeImporter,
        worker_id: str,
        started_at: datetime,
        sources: list[SourceAdapter] | None = None,
    ) -> None:
        self.factory = factory
        self.scanner = scanner
        self.stepcode = stepcode
        self.worker_id = worker_id
        self.started_at = started_at
        self.checkpoints = CheckpointRepository()
        self.linker = SessionLinker()
        self.ingestor = EventIngestor(scanner, stepcode)
        self.codex_source = CodexJsonlSource(scanner)
        self.stepcode_source = StepCodeSqliteSource(
            stepcode, parser_version=scanner.parser_version
        )
        self.sources: list[SourceAdapter] = sources or [
            self.codex_source,
            self.stepcode_source,
        ]
        self.active_sessions = 0
        self.last_error: BaseException | None = None
        self.last_cycle = CycleStats()
        #: Extra heartbeat metadata contributed by sibling tasks (local scan, …).
        self.aux_metadata: dict[str, Any] = {}
        self._heartbeat_lock = asyncio.Lock()

    #: Bounded per-cycle maintenance so a large backlog can never stall polling.
    _MAINTENANCE_BATCH = 200

    async def scan_once(self) -> int:
        """Run one planning + ingest cycle over every registered source.

        The cycle is split into a cheap **planning pass** (discover, one
        checkpoint query per chunk, ``stat`` fingerprints) and an **ingest
        pass** that only touches sources whose metadata changed.  Unchanged
        sources cost one dictionary lookup, so the loop no longer serializes a
        database round trip and a commit per source per poll.
        """
        cycle = CycleStats()
        self.last_cycle = cycle
        self.last_error = None
        started = time.perf_counter()

        pairs, duplicates = await self._collect_sources()
        cycle.discovered = len(pairs)
        cycle.duplicates_skipped = duplicates
        self.active_sessions = len(pairs)
        cycle.discover_ms = _elapsed_ms(started)

        plan_started = time.perf_counter()
        to_index, plan_failures, unchanged = await self._plan_sources(pairs)
        cycle.changed = len(to_index)
        cycle.unchanged = unchanged
        cycle.plan_ms = _elapsed_ms(plan_started)

        ingest_started = time.perf_counter()
        indexed = 0
        for adapter, source, plan in to_index:
            count, failed = await self._index_source(adapter, source, plan)
            indexed += count
            if failed:
                cycle.failures += 1
            if indexed and indexed % 10 == 0:
                await self.heartbeat()
        cycle.indexed = indexed
        cycle.ingest_ms = _elapsed_ms(ingest_started)

        for source, exc in plan_failures:
            await self._record_failure(source, exc)
            cycle.failures += 1

        maintain_started = time.perf_counter()
        cycle.maintenance = await self._maintain()
        cycle.maintain_ms = _elapsed_ms(maintain_started)

        cycle.cycle_ms = _elapsed_ms(started)
        cycle.finished = True
        await self.heartbeat(success=self.last_error is None)
        return indexed

    async def _collect_sources(
        self,
    ) -> tuple[list[tuple[SourceAdapter, SourceDescriptor]], int]:
        """Discover every source once, deduplicated by identity."""
        pairs: list[tuple[SourceAdapter, SourceDescriptor]] = []
        seen: set[str] = set()
        seen_files: set[tuple[str, str]] = set()
        duplicates = 0
        for adapter in self.sources:
            for source in await self._safe_discover(adapter, adapter.kind):
                canonical = source.path.with_suffix("") if source.compression == "zstd" else source.path
                family = "codex_jsonl" if source.kind in {"codex_jsonl", "xdcode_jsonl"} else source.kind
                file_key = (family, str(canonical.resolve()))
                duplicate_file = source.format == "jsonl" and file_key in seen_files
                if source.identity in seen or duplicate_file:
                    duplicates += 1
                    continue
                seen.add(source.identity)
                if source.format == "jsonl":
                    seen_files.add(file_key)
                pairs.append((adapter, source))
        return pairs, duplicates

    async def _plan_sources(
        self, pairs: list[tuple[SourceAdapter, SourceDescriptor]]
    ) -> tuple[
        list[tuple[SourceAdapter, SourceDescriptor, SourcePlan]],
        list[tuple[SourceDescriptor, BaseException]],
        int,
    ]:
        """Classify every source as changed/unchanged with bounded queries.

        Checkpoints are fetched in chunks and the stat-only ``plan`` call runs
        while those rows are still attached, so planning never triggers a
        query per source.
        """
        to_index: list[tuple[SourceAdapter, SourceDescriptor, SourcePlan]] = []
        failures: list[tuple[SourceDescriptor, BaseException]] = []
        unchanged = 0
        async with self.factory() as db:
            checkpoints = await self.checkpoints.get_many(
                db, [source.identity for _, source in pairs]
            )
            host_pairs = [
                (adapter, source)
                for adapter, source in pairs
                if isinstance(adapter, StepGoHostSource)
            ]
            host_runs: dict[str, StepGoRun] = {}
            if host_pairs:
                rows = await db.scalars(
                    select(StepGoRun).where(
                        StepGoRun.source_identity.in_(
                            [source.identity for _, source in host_pairs]
                        )
                    )
                )
                host_runs = {
                    row.source_identity: row for row in rows.all()
                }
            for adapter, source in pairs:
                try:
                    if isinstance(adapter, StepGoHostSource):
                        plan = adapter.plan_for_run(
                            source, host_runs.get(source.identity)
                        )
                    else:
                        plan = adapter.plan(source, checkpoints.get(source.identity))
                except Exception as exc:  # noqa: BLE001 - isolate one source
                    failures.append((source, exc))
                    continue
                if plan.action is PlanAction.UNCHANGED:
                    unchanged += 1
                    continue
                to_index.append((adapter, source, plan))
        return to_index, failures, unchanged

    async def _maintain(self) -> dict[str, int]:
        """Backfill derived state that is no longer re-derived on every poll.

        Workspace links and titles are written while ingesting a source.  This
        pass only repairs rows that are still missing them (sessions indexed
        before a field existed, or titles cleared afterwards) and upgrades
        workspace semantics written before classification was introduced.

        Every predicate here must be *terminal*: a row that this pass cannot
        repair must stop being selected, otherwise the same unrepairable rows
        would consume the batch on every cycle and starve older rows.  Titles
        are therefore only attempted for sessions that actually have a user
        message, and ``unknown`` is a final classification (it is what
        ``classify_workspace`` returns for paths it cannot verify), not a
        pending one.  Work is capped at :attr:`_MAINTENANCE_BATCH` rows per
        cycle, and a failing row or query is recorded instead of stopping the
        polling loop.
        """
        stats = {
            "workspace_linked": 0,
            "titles_backfilled": 0,
            "workspace_kind": 0,
            "cwd_normalized": 0,
            "errors": 0,
        }
        has_workspace_kind = "workspace_kind" in Workspace.__table__.columns
        async with self.factory() as db:
            try:
                unlinked = (
                    await db.scalars(
                        select(AgentSession)
                        .where(
                            AgentSession.workspace_id.is_(None),
                            AgentSession.cwd.is_not(None),
                        )
                        .order_by(AgentSession.updated_at.desc())
                        .limit(self._MAINTENANCE_BATCH)
                    )
                ).all()
                for session in unlinked:
                    try:
                        await self.linker.link_workspace(db, session)
                    except Exception:  # noqa: BLE001 - one bad row must not stop the pass
                        stats["errors"] += 1
                        continue
                    if session.workspace_id is not None:
                        stats["workspace_linked"] += 1
                    elif session.cwd is None:
                        # A blank cwd is normalized to NULL: a real change that
                        # must be committed, otherwise the row is re-selected
                        # forever (it is no longer ``cwd IS NOT NULL``).
                        stats["cwd_normalized"] += 1

                # Only sessions with a usable user message can ever be titled;
                # without this filter the message-less rows would be re-probed
                # on every cycle forever.
                untitled = (
                    await db.scalars(
                        select(AgentSession)
                        .where(
                            AgentSession.display_title.is_(None),
                            exists().where(
                                CodexMessage.agent_session_id == AgentSession.id,
                                CodexMessage.role == "user",
                                has_visible_text(CodexMessage.text),
                            ),
                        )
                        .order_by(AgentSession.updated_at.desc())
                        .limit(self._MAINTENANCE_BATCH)
                    )
                ).all()
                for session in untitled:
                    try:
                        await self.linker.backfill_title(db, session)
                    except Exception:  # noqa: BLE001 - one bad row must not stop the pass
                        stats["errors"] += 1
                        continue
                    if session.display_title:
                        stats["titles_backfilled"] += 1

                if has_workspace_kind:
                    stale = (
                        await db.scalars(
                            select(Workspace)
                            .where(
                                or_(
                                    Workspace.workspace_kind.is_(None),
                                    Workspace.workspace_kind == "",
                                )
                            )
                            .order_by(Workspace.normalized_path)
                            .limit(self._MAINTENANCE_BATCH)
                        )
                    ).all()
                    for workspace in stale:
                        try:
                            kind, confidence = self.linker.classify_workspace(
                                workspace.normalized_path
                            )
                        except Exception:  # noqa: BLE001 - isolate one path probe
                            stats["errors"] += 1
                            continue
                        workspace.workspace_kind = kind
                        workspace.workspace_confidence = confidence
                        stats["workspace_kind"] += 1

                host_source = next(
                    (
                        adapter
                        for adapter in self.sources
                        if isinstance(adapter, StepGoHostSource)
                    ),
                    None,
                )
                if host_source is not None:
                    stats["stepgo_relinked"] = await host_source.relink_pending(db)

                if any(count for key, count in stats.items() if key != "errors"):
                    await db.commit()
                else:
                    await db.rollback()
            except Exception as exc:  # noqa: BLE001 - maintenance must not kill polling
                await db.rollback()
                self.last_error = exc
                stats["errors"] += 1
        return stats

    async def _safe_discover(
        self,
        adapter: SourceAdapter,
        kind: str,
    ) -> list[SourceDescriptor]:
        try:
            return adapter.discover()
        except Exception as exc:  # noqa: BLE001 - source discovery must be isolated
            self.last_error = exc
            async with self.factory() as db:
                await audit.record(
                    db,
                    action="agent.source.discovery_failed",
                    actor_type=audit.ACTOR_SYSTEM,
                    resource_type="index_source",
                    resource_id=kind,
                    detail={"source_kind": kind},
                    result=audit.RESULT_FAILURE,
                    error_kind=type(exc).__name__,
                    error_message=str(exc),
                )
                await db.commit()
            return []

    async def _index_source(
        self,
        adapter: SourceAdapter,
        source: SourceDescriptor,
        plan: SourcePlan,
    ) -> tuple[int, bool]:
        """Ingest one source, returning ``(indexed_count, failed)``."""
        if isinstance(adapter, CodexJsonlSource):
            return await self._index_codex(adapter, source, plan)
        if isinstance(adapter, StepCodeSqliteSource):
            return await self._index_stepcode(adapter, source, plan)
        if isinstance(adapter, StepGoHostSource):
            return await self._index_stepgo_host(adapter, source, plan)
        return await self._index_external(adapter, source, plan)

    async def _index_stepgo_host(
        self,
        adapter: StepGoHostSource,
        source: SourceDescriptor,
        plan: SourcePlan,
    ) -> tuple[int, bool]:
        """Index one host log with its own cursor stored on ``stepgo_runs``."""
        async with self.factory() as db:
            try:
                run = await adapter.ingest(db, plan)
                await self._catalog_stepgo_success(db, source, run)
                await audit.record(
                    db,
                    action="agent.stepgo_run.indexed",
                    actor_type=audit.ACTOR_SYSTEM,
                    resource_type="stepgo_run",
                    resource_id=run.id,
                    detail={
                        "path": run.source_path,
                        "agent": run.agent,
                        "event_count": run.event_count,
                        "link_status": run.link_status,
                        "linked_agent_session_id": run.linked_agent_session_id,
                        "index_status": run.index_status,
                    },
                    result=(
                        audit.RESULT_SUCCESS
                        if run.index_status == "indexed"
                        else audit.RESULT_FAILURE
                    ),
                    error_kind=(
                        None if run.index_status == "indexed" else run.index_status
                    ),
                    error_message=run.last_error,
                )
                await db.commit()
                return 1, False
            except Exception as exc:  # noqa: BLE001 - isolate host-log failures
                await db.rollback()
                await self._record_failure(source, exc)
                return 0, True

    async def _index_codex(
        self,
        adapter: CodexJsonlSource,
        source: SourceDescriptor,
        plan: SourcePlan,
    ) -> tuple[int, bool]:
        """Ingest one rollout; only called for sources whose metadata changed."""
        async with self.factory() as db:
            try:
                checkpoint = await self.checkpoints.get(db, source.identity)
                if checkpoint is not None:
                    await self.checkpoints.mark_attempt(
                        checkpoint, fingerprint=plan.fingerprint
                    )
                session = (
                    await self.ingestor.ingest_codex(db, plan)
                    if adapter.scanner is self.scanner
                    else await adapter.ingest(db, plan)
                )
                await self.linker.link_workspace(db, session)
                checkpoint = await self.checkpoints.ensure(
                    db,
                    session=session,
                    source_kind=source.kind,
                    source_identity=source.identity,
                    parser_version=adapter.parser_version,
                )
                fingerprint = {
                    **plan.fingerprint,
                    "sha256": session.file_sha256,
                }
                await self.checkpoints.mark_success(
                    checkpoint,
                    cursor={
                        "byte_offset": session.last_indexed_offset,
                        "line_number": session.last_indexed_line,
                    },
                    fingerprint=fingerprint,
                    parser_version=adapter.parser_version,
                )
                await self._catalog_success(db, source, session)
                await self._audit_indexed(db, session)
                await db.commit()
                return 1, False
            except Exception as exc:  # noqa: BLE001 - isolate each rollout
                await db.rollback()
                await self._record_failure(source, exc)
                return 0, True

    async def _index_stepcode(
        self,
        adapter: StepCodeSqliteSource,
        source: SourceDescriptor,
        plan: SourcePlan,
    ) -> tuple[int, bool]:
        """Ingest one SQLite thread; only called for changed sources."""
        async with self.factory() as db:
            checkpoint = await self.checkpoints.get(db, source.identity)
            try:
                if checkpoint is not None:
                    await self.checkpoints.mark_attempt(
                        checkpoint, fingerprint=plan.fingerprint
                    )
                cursor = checkpoint.cursor_json if checkpoint is not None else {}
                session = await self.ingestor.ingest_stepcode(
                    db,
                    plan,
                    after_ordinal=(
                        int(cursor.get("rollout_ordinal", -1))
                        if plan.action == PlanAction.APPEND
                        else -1
                    ),
                )
                await self.linker.link_workspace(db, session)
                checkpoint = await self.checkpoints.ensure(
                    db,
                    session=session,
                    source_kind=source.kind,
                    source_identity=source.identity,
                    parser_version=adapter.parser_version,
                )
                state = adapter.importer.thread_state(
                    source.path, source.thread_id or ""
                )
                await self.checkpoints.mark_success(
                    checkpoint,
                    cursor={
                        "rollout_ordinal": state["max_rollout_ordinal"],
                        "updated_at_ordinal": state["max_updated_at_ordinal"],
                    },
                    fingerprint=plan.fingerprint,
                    parser_version=adapter.parser_version,
                )
                await self._catalog_success(db, source, session)
                await self._audit_indexed(db, session)
                await db.commit()
                return 1, False
            except Exception as exc:  # noqa: BLE001 - isolate live SQLite sources
                await db.rollback()
                await self._record_failure(source, exc)
                return 0, True

    async def _index_external(
        self,
        adapter: SourceAdapter,
        source: SourceDescriptor,
        plan: SourcePlan,
    ) -> tuple[int, bool]:
        """Ingest one non-Codex source; only called for changed sources."""
        async with self.factory() as db:
            checkpoint = await self.checkpoints.get(db, source.identity)
            try:
                if checkpoint is not None:
                    await self.checkpoints.mark_attempt(
                        checkpoint, fingerprint=plan.fingerprint
                    )
                ingest = getattr(adapter, "ingest", None)
                if ingest is None:
                    raise TypeError(
                        f"adapter {type(adapter).__name__} has no ingest method"
                    )
                session = await ingest(db, plan)
                await self.linker.link_workspace(db, session)
                checkpoint = await self.checkpoints.ensure(
                    db,
                    session=session,
                    source_kind=source.kind,
                    source_identity=source.identity,
                    parser_version=adapter.parser_version,
                )
                final = source.path.stat()
                fingerprint = {
                    **plan.fingerprint,
                    "size": plan.fingerprint["size"] if source.kind == "pi_jsonl" else final.st_size,
                    "mtime_ns": plan.fingerprint["mtime_ns"] if source.kind == "pi_jsonl" else final.st_mtime_ns,
                }
                await self.checkpoints.mark_success(
                    checkpoint,
                    cursor={
                        "byte_offset": session.last_indexed_offset,
                        "line_number": session.last_indexed_line,
                    },
                    fingerprint=fingerprint,
                    parser_version=adapter.parser_version,
                )
                await self._catalog_success(db, source, session)
                await self._audit_indexed(db, session)
                await db.commit()
                return 1, False
            except Exception as exc:  # noqa: BLE001 - isolate adapter failures
                await db.rollback()
                await self._record_failure(source, exc)
                return 0, True

    async def _record_failure(
        self, source: SourceDescriptor, exc: BaseException
    ) -> None:
        self.last_error = exc
        async with self.factory() as db:
            checkpoint = await self.checkpoints.get(db, source.identity)
            if checkpoint is not None:
                await self.checkpoints.mark_failure(checkpoint, exc)
            if isinstance(source.kind, str) and source.kind == "stepgo_host_jsonl":
                run = await db.scalar(
                    select(StepGoRun).where(
                        StepGoRun.source_identity == source.identity
                    )
                )
                if run is not None:
                    run.index_status = "failed"
                    run.last_error = str(exc)[:2000]
            await self._catalog_failure(db, source, exc)
            await audit.record(
                db,
                action="agent.session.index_failed",
                actor_type=audit.ACTOR_SYSTEM,
                resource_type="index_source",
                resource_id=source.identity,
                detail={"path": str(source.path), "source_kind": source.kind},
                result=audit.RESULT_FAILURE,
                error_kind=type(exc).__name__,
                error_message=str(exc),
            )
            await db.commit()

    @staticmethod
    async def _catalog_stepgo_success(
        db: AsyncSession,
        source: SourceDescriptor,
        run: StepGoRun,
    ) -> None:
        row = await db.scalar(
            select(LocalDataSource).where(
                LocalDataSource.source_identity == source.identity
            )
        )
        if row is None:
            row = LocalDataSource(
                id=(
                    "source-"
                    + hashlib.sha256(source.identity.encode()).hexdigest()[:48]
                ),
                agent=source.agent,
                source_kind=source.kind,
                source_identity=source.identity,
                source_path=run.source_path,
                source_format=source.format,
                parser_version=run.parser_version,
            )
            db.add(row)
        row.agent = source.agent
        row.source_kind = source.kind
        row.source_path = run.source_path
        row.source_format = source.format
        row.parser_version = run.parser_version
        row.status = run.index_status
        row.record_count = run.event_count
        row.byte_size = run.file_size
        row.coverage_started_at = run.start_time
        row.coverage_ended_at = run.end_time
        row.last_attempt_at = datetime.now(UTC)
        if run.index_status == "indexed":
            row.last_success_at = datetime.now(UTC)
        row.last_error_kind = None if run.index_status == "indexed" else run.index_status
        row.last_error = run.last_error
        await db.flush()

    @staticmethod
    async def _catalog_success(
        db: AsyncSession,
        source: SourceDescriptor,
        session: AgentSession,
    ) -> None:
        row = await db.scalar(
            select(LocalDataSource).where(
                LocalDataSource.source_identity == source.identity
            )
        )
        if row is None:
            row = LocalDataSource(
                id=(
                    "source-"
                    + hashlib.sha256(source.identity.encode()).hexdigest()[:48]
                ),
                agent=source.agent,
                source_kind=source.kind,
                source_identity=source.identity,
                source_path=str(source.path.resolve(strict=False)),
                source_format=source.format,
                parser_version=session.parser_version,
            )
            db.add(row)
        row.agent = source.agent
        row.source_kind = source.kind
        row.source_path = str(source.path.resolve(strict=False))
        row.source_format = source.format
        row.parser_version = session.parser_version
        row.status = session.index_status
        row.record_count = session.last_indexed_line
        row.byte_size = session.file_size
        row.coverage_started_at = session.start_time
        row.coverage_ended_at = session.end_time
        row.last_attempt_at = datetime.now(UTC)
        row.last_success_at = datetime.now(UTC)
        row.last_error_kind = None
        row.last_error = None
        await db.flush()

    @staticmethod
    async def _catalog_failure(
        db: AsyncSession,
        source: SourceDescriptor,
        exc: BaseException,
    ) -> None:
        row = await db.scalar(
            select(LocalDataSource).where(
                LocalDataSource.source_identity == source.identity
            )
        )
        if row is None:
            row = LocalDataSource(
                id=(
                    "source-"
                    + hashlib.sha256(source.identity.encode()).hexdigest()[:48]
                ),
                agent=source.agent,
                source_kind=source.kind,
                source_identity=source.identity,
                source_path=str(source.path.resolve(strict=False)),
                source_format=source.format,
                parser_version="unknown",
            )
            db.add(row)
        row.status = "degraded"
        row.last_attempt_at = datetime.now(UTC)
        row.last_error_kind = type(exc).__name__[:64]
        row.last_error = str(exc)[:2000]
        with suppress(OSError):
            row.byte_size = source.path.stat().st_size
        await db.flush()

    @staticmethod
    async def _audit_indexed(db: AsyncSession, session: AgentSession) -> None:
        await audit.record(
            db,
            action="agent.session.indexed",
            actor_type=audit.ACTOR_SYSTEM,
            resource_type="agent_session",
            resource_id=session.id,
            detail={
                "path": session.source_path or session.rollout_path,
                "source_kind": session.source_kind,
                "agent": session.agent,
                "index_status": session.index_status,
                "last_indexed_line": session.last_indexed_line,
            },
            result=(
                audit.RESULT_SUCCESS
                if session.index_status == "indexed"
                else audit.RESULT_FAILURE
            ),
            error_kind=(None if session.index_status == "indexed" else session.index_status),
            error_message=session.last_error,
        )

    async def heartbeat(self, *, success: bool | None = None) -> None:
        async with self._heartbeat_lock, self.factory() as db:
            now = datetime.now(UTC)
            metadata = {
                "sessions_root": str(self.scanner.root),
                "detected_agents": discovery.detected_agents(
                    layouts=settings().discovery_roots
                ),
                "registered_sources": [
                    {
                        "kind": adapter.kind,
                        "agent": adapter.agent,
                        "root": str(
                            getattr(
                                adapter,
                                "root",
                                getattr(
                                    getattr(adapter, "scanner", None),
                                    "root",
                                    getattr(
                                        getattr(adapter, "importer", None),
                                        "root",
                                        "",
                                    ),
                                ),
                            )
                        ),
                    }
                    for adapter in self.sources
                ],
                "last_cycle": self.last_cycle.as_dict(),
                "scanner": dict(getattr(self.scanner, "stats", {})),
            }
            metadata.update(self.aux_metadata)
            row = await db.get(WorkerHeartbeat, self.worker_id)
            if row is None:
                row = WorkerHeartbeat(
                    worker_id=self.worker_id,
                    worker_type="indexer",
                    hostname=socket.gethostname(),
                    pid=os.getpid(),
                    active_sessions=self.active_sessions,
                    metadata_json=metadata,
                    started_at=self.started_at,
                    heartbeat_at=now,
                )
                db.add(row)
            else:
                row.active_sessions = self.active_sessions
                row.heartbeat_at = now
                row.metadata_json = metadata
            if success is True:
                row.status = "running"
                row.last_success_at = now
                row.last_error_kind = None
                row.last_error = None
                self.last_error = None
            elif self.last_error is not None:
                row.status = "degraded"
                row.last_error_kind = type(self.last_error).__name__[:64]
                row.last_error = str(self.last_error)[:2000]
            await db.commit()


__all__ = ["IndexerOrchestrator"]
