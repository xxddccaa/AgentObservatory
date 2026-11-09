"""Durable index checkpoint persistence."""
from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import Any, TypeVar, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_observatory.db.models import AgentSession, IndexCheckpoint


_LOOKUP_CHUNK = 400
_T = TypeVar("_T")


def _chunked(values: Iterable[_T], size: int) -> Iterator[list[_T]]:
    """Yield ``size``-bounded batches so ``IN`` clauses stay well-formed."""
    batch: list[_T] = []
    for value in values:
        batch.append(value)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


class CheckpointRepository:
    async def get(
        self, db: AsyncSession, source_identity: str
    ) -> IndexCheckpoint | None:
        return cast(
            IndexCheckpoint | None,
            await db.scalar(
                select(IndexCheckpoint).where(
                    IndexCheckpoint.source_identity == source_identity
                )
            ),
        )

    @staticmethod
    async def get_many(
        db: AsyncSession, source_identities: Iterable[str]
    ) -> dict[str, IndexCheckpoint]:
        """Load checkpoints for many sources with one query per chunk.

        A full cycle touches every discovered source (hundreds to thousands);
        fetching each checkpoint individually dominated the polling loop, so
        callers plan against this batch view instead.
        """
        found: dict[str, IndexCheckpoint] = {}
        for chunk in _chunked(source_identities, _LOOKUP_CHUNK):
            rows = await db.scalars(
                select(IndexCheckpoint).where(
                    IndexCheckpoint.source_identity.in_(chunk)
                )
            )
            for row in rows:
                found[row.source_identity] = row
        return found

    async def ensure(
        self,
        db: AsyncSession,
        *,
        session: AgentSession,
        source_kind: str,
        source_identity: str,
        parser_version: str,
    ) -> IndexCheckpoint:
        checkpoint = await self.get(db, source_identity)
        if checkpoint is None:
            checkpoint = IndexCheckpoint(
                id=f"checkpoint-{hashlib.sha256(source_identity.encode()).hexdigest()[:48]}",
                source_kind=source_kind,
                source_identity=source_identity,
                agent_session_id=session.id,
                parser_version=parser_version,
                cursor_json={},
                fingerprint_json={},
            )
            db.add(checkpoint)
            await db.flush()
        return checkpoint

    async def mark_attempt(
        self, checkpoint: IndexCheckpoint, *, fingerprint: dict[str, Any]
    ) -> None:
        checkpoint.status = "indexing"
        checkpoint.fingerprint_json = fingerprint
        checkpoint.last_attempt_at = datetime.now(UTC)
        checkpoint.last_error_kind = None
        checkpoint.last_error = None

    async def mark_success(
        self,
        checkpoint: IndexCheckpoint,
        *,
        cursor: dict[str, Any],
        fingerprint: dict[str, Any],
        parser_version: str,
    ) -> None:
        now = datetime.now(UTC)
        checkpoint.cursor_json = cursor
        checkpoint.fingerprint_json = fingerprint
        checkpoint.parser_version = parser_version
        checkpoint.status = "indexed"
        checkpoint.last_attempt_at = now
        checkpoint.last_success_at = now
        checkpoint.last_error_kind = None
        checkpoint.last_error = None

    async def mark_failure(
        self, checkpoint: IndexCheckpoint, exc: BaseException
    ) -> None:
        checkpoint.status = "failed"
        checkpoint.last_attempt_at = datetime.now(UTC)
        checkpoint.last_error_kind = type(exc).__name__[:64]
        checkpoint.last_error = str(exc)[:2000]


__all__ = ["CheckpointRepository"]
