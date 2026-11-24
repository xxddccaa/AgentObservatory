"""Shared helpers for read-only session adapters."""
from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from agent_observatory.db.models import (
    ActivitySpan,
    AgentEvent,
    AgentTurn,
    CodexMessage,
    ToolCall,
)

from ..fingerprints import file_fingerprint, indexed_content_matches
from ..tree_cache import cached_resolve
from .base import PlanAction, SourceDescriptor, SourcePlan

MAX_LINE_BYTES = 8 * 1024 * 1024
ONGOING_SECONDS = 600


def stable_id(prefix: str, value: str) -> str:
    """Return a deterministic 48-char hex identifier for a synthetic row."""
    return hashlib.sha256(f"{prefix}:{value}".encode()).hexdigest()[:48]


def pg_safe(value: Any) -> Any:
    """Escape NUL bytes recursively because PostgreSQL refuses them in text/JSONB."""
    if isinstance(value, str):
        return value.replace("\x00", "\\u0000")
    if isinstance(value, dict):
        return {str(pg_safe(key)): pg_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [pg_safe(item) for item in value]
    return value


def parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 string or epoch second/millisecond number as UTC."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if abs(value) > 100_000_000_000 else value
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def as_int(value: Any, *, non_negative: bool = True) -> int:
    """Safely coerce an arbitrary JSON field to an integer."""
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed) if non_negative else parsed


def duration_ms(started_at: datetime | None, ended_at: datetime | None) -> int | None:
    """Return elapsed milliseconds between two timestamps when both are present."""
    if started_at is None or ended_at is None:
        return None
    return max(0, int((ended_at - started_at).total_seconds() * 1000))


async def clear_session_rows(
    db: AsyncSession,
    session_id: str,
    *,
    include_events: bool = True,
    synchronize_session: Literal["auto", "evaluate", "fetch"] | Literal[False] = "evaluate",
) -> None:
    """Remove projected child rows (and optionally raw events) before a rebuild."""
    models: tuple[type[Any], ...] = (
        (ActivitySpan, CodexMessage, ToolCall, AgentTurn, AgentEvent)
        if include_events
        else (ActivitySpan, CodexMessage, ToolCall, AgentTurn)
    )
    for model in models:
        await db.execute(
            delete(model)
            .where(model.agent_session_id == session_id)
            .execution_options(synchronize_session=synchronize_session)
        )


@dataclass(frozen=True, slots=True)
class RawJsonlLine:
    """One bounded line slice scanned from a UTF-8 JSONL file."""

    byte_offset: int
    byte_length: int
    terminated: bool
    raw: bytes | None
    prefix: bytes
    oversized_hash: str | None


def iter_raw_jsonl_lines(
    path: str | Path,
    *,
    offset: int = 0,
    max_line_bytes: int = MAX_LINE_BYTES,
    max_bytes: int | None = None,
    prefix_bytes: int = 1024,
) -> Iterator[RawJsonlLine]:
    """Stream lines from ``offset`` without buffering lines above ``max_line_bytes``.

    When a line exceeds ``max_line_bytes``, only a short ``prefix`` is retained
    while the full payload is hashed chunk-by-chunk.  When EOF is reached on a
    line without a trailing newline, ``terminated=False`` is yielded once (with
    ``raw=None`` if oversized) so the caller can decide whether to consume a
    complete object or wait for the writer to finish flushing the line.
    """
    consumed = 0
    with Path(path).open("rb") as handle:
        handle.seek(offset)
        while True:
            if max_bytes is not None and consumed >= max_bytes:
                return
            byte_offset = handle.tell()
            chunks: list[bytes] = []
            line_size = 0
            too_large = False
            prefix = b""
            digest: Any | None = None
            terminated = False
            while True:
                chunk = handle.readline(64 * 1024)
                if not chunk:
                    if not chunks and not too_large:
                        return
                    break
                line_size += len(chunk)
                if too_large:
                    assert digest is not None
                    digest.update(chunk)
                elif line_size > max_line_bytes:
                    too_large = True
                    prefix = (b"".join(chunks) + chunk)[:prefix_bytes]
                    digest = hashlib.sha256()
                    for item in chunks:
                        digest.update(item)
                    digest.update(chunk)
                    chunks.clear()
                else:
                    chunks.append(chunk)
                if chunk.endswith(b"\n"):
                    terminated = True
                    break
            consumed += line_size
            if too_large:
                assert digest is not None
                yield RawJsonlLine(
                    byte_offset=byte_offset,
                    byte_length=line_size,
                    terminated=terminated,
                    raw=None,
                    prefix=prefix,
                    oversized_hash=digest.hexdigest(),
                )
                continue
            raw = b"".join(chunks)
            yield RawJsonlLine(
                byte_offset=byte_offset,
                byte_length=line_size,
                terminated=terminated,
                raw=raw,
                prefix=raw[:prefix_bytes],
                oversized_hash=None,
            )


def plan_stat_rebuild(
    source: SourceDescriptor,
    checkpoint: Any | None,
    parser_version: str,
    *,
    extra_fp: dict[str, Any] | None = None,
) -> SourcePlan:
    """Plan UNCHANGED/REBUILD from file size, mtime_ns, and optional extra fingerprint keys."""
    stat = source.path.stat()
    fingerprint: dict[str, Any] = {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "path": str(source.path.resolve()),
    }
    if extra_fp:
        fingerprint.update(extra_fp)
    if checkpoint is None or getattr(checkpoint, "parser_version", None) != parser_version:
        return SourcePlan(source, PlanAction.REBUILD, fingerprint)
    previous = getattr(checkpoint, "fingerprint_json", {}) or {}
    if (
        getattr(checkpoint, "status", None) == "indexed"
        and previous.get("size") == stat.st_size
        and previous.get("mtime_ns") == stat.st_mtime_ns
        and (
            not extra_fp
            or all(previous.get(key) == value for key, value in extra_fp.items())
        )
    ):
        return SourcePlan(source, PlanAction.UNCHANGED, fingerprint)
    return SourcePlan(source, PlanAction.REBUILD, fingerprint)


def plan_append_jsonl(
    source: SourceDescriptor,
    checkpoint: Any | None,
    parser_version: str,
    *,
    extra_fp: dict[str, Any] | None = None,
    check_path: bool = True,
    check_prefix: bool = True,
    rebuild_on_same_size: bool = True,
    cached_path: bool = False,
) -> SourcePlan:
    """Plan UNCHANGED / APPEND / REBUILD for byte-offset-appendable JSONL files."""
    stat = source.path.stat()
    resolved = str(cached_resolve(source.path) if cached_path else source.path.resolve())
    fingerprint: dict[str, Any] = {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "path": resolved,
    }
    if extra_fp:
        fingerprint.update(extra_fp)

    def _full_fp() -> dict[str, Any]:
        return {**fingerprint, **file_fingerprint(source.path)} if check_prefix else fingerprint

    if checkpoint is None or getattr(checkpoint, "parser_version", None) != parser_version:
        return SourcePlan(source, PlanAction.REBUILD, _full_fp())
    previous = getattr(checkpoint, "fingerprint_json", {}) or {}
    cursor = getattr(checkpoint, "cursor_json", {}) or {}
    if (
        (check_path and previous.get("path") != fingerprint["path"])
        or stat.st_size < as_int(cursor.get("byte_offset"))
        or (
            extra_fp is not None
            and any(
                previous.get(key) != value
                for key, value in extra_fp.items()
                if key != "meta"
            )
        )
    ):
        return SourcePlan(source, PlanAction.REBUILD, _full_fp())
    if (
        getattr(checkpoint, "status", None) == "indexed"
        and stat.st_size == previous.get("size")
        and stat.st_mtime_ns == previous.get("mtime_ns")
        and (
            not extra_fp
            or all(previous.get(key) == value for key, value in extra_fp.items())
        )
    ):
        return SourcePlan(source, PlanAction.UNCHANGED, fingerprint)
    if check_prefix:
        fingerprint.update(file_fingerprint(source.path))
        if not indexed_content_matches(source.path, previous):
            return SourcePlan(source, PlanAction.REBUILD, fingerprint)
    if rebuild_on_same_size and stat.st_size <= as_int(previous.get("size")):
        return SourcePlan(source, PlanAction.REBUILD, fingerprint)
    return SourcePlan(source, PlanAction.APPEND, fingerprint)


__all__ = [
    "MAX_LINE_BYTES",
    "ONGOING_SECONDS",
    "RawJsonlLine",
    "as_int",
    "clear_session_rows",
    "duration_ms",
    "iter_raw_jsonl_lines",
    "parse_ts",
    "pg_safe",
    "plan_append_jsonl",
    "plan_stat_rebuild",
    "stable_id",
]
