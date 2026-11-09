"""Bounded, forward-compatible parser for Claude Code JSONL transcripts."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ClaudeRecord:
    line_number: int
    byte_offset: int
    raw_json: dict[str, Any]
    raw_hash: str
    timestamp: datetime | None
    byte_length: int = 0

    @property
    def record_type(self) -> str:
        return str(self.raw_json.get("type") or "unknown")


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _sanitize(value: Any) -> Any:
    # PostgreSQL rejects literal NULs in JSON/text values.
    if isinstance(value, str):
        return value.replace("\x00", "\\u0000")
    if isinstance(value, dict):
        return {_sanitize(k): _sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    return value


def parse_line(line: str, *, line_number: int = 0, byte_offset: int = 0) -> ClaudeRecord:
    raw = json.loads(line)
    if not isinstance(raw, dict):
        raise ValueError("Claude event must be a JSON object")
    return ClaudeRecord(
        line_number=line_number,
        byte_offset=byte_offset,
        raw_json=_sanitize(raw),
        raw_hash=hashlib.sha256(line.encode("utf-8")).hexdigest(),
        timestamp=_timestamp(raw.get("timestamp")),
        byte_length=len(line.encode("utf-8")),
    )


def iter_records(
    path: str | Path,
    *,
    offset: int = 0,
    line_offset: int = 0,
    max_bytes: int | None = None,
) -> Iterator[ClaudeRecord]:
    """Yield complete records from ``path`` without unbounded buffering.

    ``offset`` is an exact byte position in the UTF-8 file. A final partial
    line is ignored so the next poll can retry it after the writer completes.
    """
    consumed = 0
    max_line_bytes = 8 * 1024 * 1024
    with Path(path).open("rb") as handle:
        handle.seek(offset)
        line_number = line_offset
        while True:
            if max_bytes is not None and consumed >= max_bytes:
                return
            byte_offset = handle.tell()
            chunks: list[bytes] = []
            line_size = 0
            while True:
                chunk = handle.readline(64 * 1024)
                if not chunk:
                    if not chunks:
                        return
                    raw = b"".join(chunks)
                    break
                chunks.append(chunk)
                line_size += len(chunk)
                if line_size > max_line_bytes:
                    while chunk and not chunk.endswith((b"\n", b"\r")):
                        chunk = handle.readline(64 * 1024)
                    line_number += 1
                    raw = b""
                    break
                if chunk.endswith((b"\n", b"\r")):
                    raw = b"".join(chunks)
                    break
            consumed += line_size
            if not raw:
                continue
            if not raw.endswith(b"\n") and not raw.endswith(b"\r"):
                return
            try:
                text = raw.decode("utf-8")
                record = parse_line(text, line_number=line_number + 1, byte_offset=byte_offset)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                line_number += 1
                continue
            line_number = record.line_number
            yield record


def content_blocks(record: ClaudeRecord) -> list[dict[str, Any]]:
    message = record.raw_json.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


__all__ = ["ClaudeRecord", "content_blocks", "iter_records", "parse_line"]
