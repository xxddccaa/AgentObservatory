"""Long-running indexer worker lifecycle."""
from __future__ import annotations

import asyncio
import logging
import os
import socket
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_observatory.config import settings
from agent_observatory.db.models import LocalDataSource
from agent_observatory.services import audit

from .linking import SessionLinker
from .local_sources import LocalInsightsIndexer
from .memories import MemoryIndexer
from .orchestrator import IndexerOrchestrator
from .registry import build_registry
from .scanner import RolloutScanner
from .stepcode import StepCodeImporter

logger = logging.getLogger(__name__)


def _error_summary(exc: BaseException | None) -> dict[str, str] | None:
    """JSON-safe error summary for heartbeat metadata.

    The heartbeat is written to a JSONB column, so the exception *object*
    cannot be published: binding it fails with ``TypeError`` and takes the
    heartbeat task (and with it the worker) down.
    """
    if exc is None:
        return None
    return {"kind": type(exc).__name__, "message": str(exc)[:500]}


class IndexerWorker:
    """Thin lifecycle wrapper around :class:`IndexerOrchestrator`."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        sessions_root: str | Path | None = None,
        worker_id: str | None = None,
        poll_interval: float | None = None,
    ) -> None:
        self.factory = factory
        self.scanner = RolloutScanner(sessions_root or settings().sessions_root)
        stepcode_root = (
            settings().stepcode_root
            if sessions_root is None
            else "/__agent_observatory_stepcode_disabled__"
        )
        self.stepcode = StepCodeImporter(stepcode_root)
        self.worker_id = worker_id or f"indexer-{socket.gethostname()}-{os.getpid()}"
        self.poll_interval = poll_interval or settings().indexer_poll_interval
        self._stop = asyncio.Event()
        #: Serializes the rollout scan and the local-source scan.  Both write
        #: ``agent_sessions`` / ``agent_turns`` / evidence rows for the same
        #: sessions, and with independent loop tasks they locked those rows in
        #: different orders and deadlocked PostgreSQL (one cycle aborted with
        #: ``DeadlockDetectedError`` every ~30s on the dev instance).  The lock
        #: keeps the two cadences independent without letting them interleave
        #: their writes.
        self._write_lock = asyncio.Lock()
        self._started_at = datetime.now(UTC)
        #: Latest local-source failure, surfaced through the heartbeat metadata
        #: instead of the orchestrator's health flag (the main scan loop owns
        #: that flag and is written concurrently).
        self.last_local_error: BaseException | None = None
        self.local_insights = (
            LocalInsightsIndexer(
                settings().codex_home,
                backfill_days=settings().insights_backfill_days,
            )
            if sessions_root is None and settings().insights_enabled
            else None
        )
        self.memory_indexer = (
            MemoryIndexer(
                codex_home=settings().codex_home,
                stepcode_home=settings().stepcode_root,
                claude_projects_root=settings().claude_projects_root,
            )
            if sessions_root is None and settings().memories_enabled
            else None
        )
        self.orchestrator = IndexerOrchestrator(
            factory,
            scanner=self.scanner,
            stepcode=self.stepcode,
            worker_id=self.worker_id,
            started_at=self._started_at,
            sources=(
                build_registry(
                    settings(), scanner=self.scanner, stepcode=self.stepcode
                )
                if sessions_root is None and settings().discovery_enabled
                else None
            ),
        )

    @property
    def _active_sessions(self) -> int:
        return self.orchestrator.active_sessions

    async def run(self) -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(self._scan_loop())
            group.create_task(self._local_loop())
            group.create_task(self._heartbeat_loop())

    async def _scan_loop(self) -> None:
        """Poll rollout / SQLite session sources at the configured interval.

        An unexpected cycle failure is recorded on the orchestrator (so the
        heartbeat flips to ``degraded``) and the loop keeps polling instead of
        tearing down its sibling tasks.
        """
        while not self._stop.is_set():
            try:
                async with self._write_lock:
                    await self.orchestrator.scan_once()
            except Exception as exc:  # noqa: BLE001 - one bad cycle must not stop polling
                self.orchestrator.last_error = exc
                logger.warning("indexer cycle failed: %s", exc)
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)

    async def _local_loop(self) -> None:
        """Poll fixed local Codex sources on their own cadence.

        This work used to share the main scan loop, where a slow rollout pass
        silently stretched the local-source interval to the whole cycle time.
        Running it as a sibling task keeps both cadences honest and leaves the
        cheap scan loop free to keep polling.
        """
        if self.local_insights is None and self.memory_indexer is None:
            return
        interval = max(settings().local_source_poll_interval, 0.001)
        while not self._stop.is_set():
            started = asyncio.get_running_loop().time()
            try:
                async with self._write_lock:
                    changed = await self.scan_local_once()
            except Exception as exc:  # noqa: BLE001 - keep the sibling tasks alive
                # ``scan_local_once`` isolates its own failures, but its
                # recovery path (rollback + audit commit) can fail too when the
                # database is unreachable.  A local source must never take the
                # whole worker down.
                self.last_local_error = exc
                changed = 0
                logger.warning("local scan failed: %s", exc)
            elapsed = asyncio.get_running_loop().time() - started
            self.orchestrator.aux_metadata["local_scan"] = {
                "changed_sources": changed,
                "duration_ms": round(elapsed * 1000, 1),
                "error": _error_summary(self.last_local_error),
            }
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop.wait(), timeout=max(0.0, interval - elapsed)
                )

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.orchestrator.heartbeat()
            except Exception as exc:  # noqa: BLE001 - liveness reporting is best effort
                logger.warning("worker heartbeat failed: %s", exc)
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop.wait(), timeout=settings().worker_heartbeat_interval
                )

    async def stop(self) -> None:
        self._stop.set()

    async def scan_once(self) -> int:
        """Run one rollout pass plus one local-source pass (single writer)."""
        async with self._write_lock:
            indexed = await self.orchestrator.scan_once()
            return indexed + await self.scan_local_once()

    async def scan_local_once(self) -> int:
        if self.local_insights is None and self.memory_indexer is None:
            return 0
        self.last_local_error = None
        async with self.factory() as db:
            try:
                before_rows = list((await db.scalars(select(LocalDataSource))).all())
                before = {
                    row.source_kind: (row.status, dict(row.fingerprint_json or {}))
                    for row in before_rows
                }
                results: dict[str, int] = {}
                if self.local_insights is not None:
                    results.update(await self.local_insights.ingest_all(db))
                if self.memory_indexer is not None:
                    results.update(await self.memory_indexer.ingest_all(db))
                after_rows = list((await db.scalars(select(LocalDataSource))).all())
                changed = [
                    row.source_kind
                    for row in after_rows
                    if before.get(row.source_kind)
                    != (row.status, dict(row.fingerprint_json or {}))
                ]
                if changed:
                    await audit.record(
                        db,
                        action="codex.local_sources.indexed",
                        actor_type=audit.ACTOR_SYSTEM,
                        resource_type="local_data_sources",
                        resource_id=self.worker_id,
                        detail={
                            "changed_sources": sorted(changed),
                            "record_counts": results,
                            "backfill_days": settings().insights_backfill_days,
                        },
                    )
                await db.commit()
                return len(changed)
            except Exception as exc:  # noqa: BLE001 - isolate local insights
                await db.rollback()
                await audit.record(
                    db,
                    action="codex.local_sources.index_failed",
                    actor_type=audit.ACTOR_SYSTEM,
                    resource_type="local_data_sources",
                    resource_id=self.worker_id,
                    detail={"codex_home": settings().codex_home},
                    result=audit.RESULT_FAILURE,
                    error_kind=type(exc).__name__,
                    error_message=str(exc),
                )
                await db.commit()
                self.last_local_error = exc
                return 0

    # Compatibility shims for callers that used the former monolithic worker.
    _normalize_cwd = staticmethod(SessionLinker.normalize_cwd)


__all__ = ["IndexerWorker"]
