"""Long-running indexer worker lifecycle."""
from __future__ import annotations

import asyncio
import ctypes
import gc
import logging
import os
import socket
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_observatory.config import settings
from agent_observatory.db.models import LocalDataSource
from agent_observatory.services import audit

from .adapters import StepCodeImporter
from .linking import SessionLinker
from .local_sources import LocalInsightsIndexer
from .memories import MemoryIndexer
from .orchestrator import IndexerOrchestrator
from .registry import build_registry
from .scanner import RolloutScanner

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


@lru_cache(maxsize=1)
def _malloc_trim() -> Callable[[int], int] | None:
    try:
        trim: Callable[[int], int] = ctypes.CDLL("libc.so.6").malloc_trim
    except (OSError, AttributeError):  # musl / non-Linux: nothing to trim
        return None
    return trim


#: The local loop runs a real pass (database included) at least this often.
_LOCAL_FULL_INTERVAL = 60.0
_RACY_WINDOW = 2.0


def _mtimes(value: Any) -> list[int]:
    """Every ``*mtime*`` integer inside a fingerprint structure."""
    if isinstance(value, dict):
        found: list[int] = []
        for key, item in value.items():
            if "mtime" in str(key) and isinstance(item, int):
                found.append(item)
            else:
                found.extend(_mtimes(item))
        return found
    if isinstance(value, (list, tuple)):
        return [mtime for item in value for mtime in _mtimes(item)]
    return []


def release_memory() -> None:
    """Hand heap freed by an ingest burst back to the operating system.

    CPython drops parsed rows promptly, but glibc keeps the freed pages mapped,
    so one large backfill would otherwise pin its peak RSS for the lifetime of
    the process.  Called only after cycles that actually ingested something.
    """
    gc.collect()
    trim = _malloc_trim()
    if trim is not None:
        trim(0)


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

        Every poll is a fast cycle (see ``IndexerOrchestrator.scan_once``)
        except one full cycle every ``FULL_CYCLE_INTERVAL`` seconds, starting
        with the first.  An unexpected cycle failure is recorded on the
        orchestrator (so the heartbeat flips to ``degraded``) and the loop
        keeps polling instead of tearing down its sibling tasks.
        """
        loop = asyncio.get_running_loop()
        next_full = loop.time()
        while not self._stop.is_set():
            indexed = 0
            full = loop.time() >= next_full
            if full:
                next_full = loop.time() + self.orchestrator.FULL_CYCLE_INTERVAL
            try:
                async with self._write_lock:
                    indexed = await self.orchestrator.scan_once(full=full)
            except Exception as exc:  # noqa: BLE001 - one bad cycle must not stop polling
                self.orchestrator.last_error = exc
                logger.warning("indexer cycle failed: %s", exc)
            if indexed:
                release_memory()
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
        loop = asyncio.get_running_loop()
        next_full = loop.time()
        settled: tuple[Any, ...] | None = None
        while not self._stop.is_set():
            started = loop.time()
            signature = self._local_signature()
            if loop.time() < next_full and signature is not None and signature == settled:
                # Every local file is exactly as the last clean pass saw it,
                # so that pass's per-source gates would all skip anyway; do
                # not spend a dozen queries and a commit to find that out.
                self.orchestrator.aux_metadata["local_scan"] = {
                    "changed_sources": 0,
                    "duration_ms": round((loop.time() - started) * 1000, 1),
                    "error": _error_summary(self.last_local_error),
                }
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=max(0.0, interval - (loop.time() - started))
                    )
                continue
            next_full = started + _LOCAL_FULL_INTERVAL
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
            if changed:
                release_memory()
            settled = (
                signature
                if signature is not None
                and self.last_local_error is None
                and not any(
                    abs(time.time() - mtime / 1e9) < _RACY_WINDOW
                    for mtime in _mtimes(signature)
                )
                else None
            )
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

    def _local_signature(self) -> tuple[Any, ...] | None:
        """Stat-only fingerprints of every local source, as their own gates
        compute them; ``None`` when they cannot be computed."""
        parts: list[Any] = []
        try:
            if self.local_insights is not None:
                for kind, path in self.local_insights.paths.items():
                    parts.append(
                        (kind, self.local_insights._fingerprint(path) if path.exists() else None)
                    )
            if self.memory_indexer is not None:
                for _agent, kind, path, _format in self.memory_indexer.sources:
                    parts.append(
                        (kind, self.memory_indexer._fingerprint(kind, path) if path.exists() else None)
                    )
        except (AttributeError, OSError):
            return None
        return tuple(parts)

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
