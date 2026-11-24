"""Bounded, forward-compatible reader for Pi session JSONL files.

Pi stores a small JSON object per line.  Message entries carry ``id`` and
``parentId`` and can form a tree, so callers must not blindly concatenate every
message in a file.  This module only parses and preserves records; branch
selection lives in :mod:`pi`.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .common import MAX_LINE_BYTES, iter_raw_jsonl_lines, pg_safe
from .common import parse_ts as timestamp

KNOWN_PI_TYPES = {
    "session",
    "session_end",
    "session_shutdown",
    "message",
    "model_change",
    "thinking_level_change",
    "compaction",
    "compaction_summary",
    "branch_summary",
    "session_name",
    "session_info",
    "custom",
}

_MAX_LINE_BYTES = MAX_LINE_BYTES


@dataclass(frozen=True, slots=True)
class PiRecord:
    line_number: int
    byte_offset: int
    byte_length: int
    raw_json: dict[str, Any]
    raw_hash: str
    event_timestamp: datetime | None

    @property
    def record_type(self) -> str:
        return str(self.raw_json.get("type") or "unknown")

    @property
    def record_id(self) -> str | None:
        value = self.raw_json.get("id")
        return str(value) if value is not None else None

    @property
    def parent_id(self) -> str | None:
        value = self.raw_json.get("parentId")
        return str(value) if value is not None else None

    @property
    def payload_type(self) -> str:
        message = self.raw_json.get("message")
        if isinstance(message, dict) and message.get("role") is not None:
            return str(message["role"])
        return self.record_type

    @property
    def known(self) -> bool:
        if self.record_type in KNOWN_PI_TYPES:
            return True
        message = self.raw_json.get("message")
        return isinstance(message, dict) and str(message.get("role") or "") in {
            "user",
            "assistant",
            "system",
            "toolResult",
        }


def parse_line(
    line: str, *, line_number: int = 0, byte_offset: int = 0
) -> PiRecord:
    raw = json.loads(line)
    if not isinstance(raw, dict):
        raise ValueError("Pi event must be a JSON object")
    encoded = line.encode("utf-8")
    message = raw.get("message")
    message_ts = message.get("timestamp") if isinstance(message, dict) else None
    return PiRecord(
        line_number=line_number,
        byte_offset=byte_offset,
        byte_length=len(encoded),
        raw_json=pg_safe(raw),
        raw_hash=hashlib.sha256(encoded).hexdigest(),
        event_timestamp=timestamp(raw.get("timestamp") or raw.get("ts") or message_ts),
    )


def _parse_error_record(
    raw: bytes, *, line_number: int, byte_offset: int
) -> PiRecord:
    text = raw.decode("utf-8", "replace").rstrip("\r\n")
    return PiRecord(
        line_number=line_number,
        byte_offset=byte_offset,
        byte_length=len(raw),
        raw_json={
            "type": "parse_error",
            "raw": text,
            "error": "invalid_json",
        },
        raw_hash=hashlib.sha256(raw).hexdigest(),
        event_timestamp=None,
    )


def iter_records(
    path: str | Path,
    *,
    offset: int = 0,
    line_offset: int = 0,
    max_bytes: int | None = None,
) -> Iterator[PiRecord]:
    """Yield complete records from ``offset`` without unbounded buffering.

    A final line without a newline is left for the next poll.  A complete but
    malformed line is preserved as a synthetic ``parse_error`` record, so the
    cursor can advance and no bytes silently disappear.
    """
    line_number = line_offset
    for item in iter_raw_jsonl_lines(
        path,
        offset=offset,
        max_line_bytes=_MAX_LINE_BYTES,
        max_bytes=max_bytes,
    ):
        if not item.terminated:
            # EOF without a newline means the writer is still appending.
            return
        if item.oversized_hash is not None:
            line_number += 1
            yield PiRecord(
                line_number=line_number,
                byte_offset=item.byte_offset,
                byte_length=item.byte_length,
                raw_json={
                    "type": "parse_error",
                    "error": "line_too_large",
                    "bytes": item.byte_length,
                },
                raw_hash=item.oversized_hash,
                event_timestamp=None,
            )
            continue
        assert item.raw is not None
        try:
            record = parse_line(
                item.raw.decode("utf-8"),
                line_number=line_number + 1,
                byte_offset=item.byte_offset,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            line_number += 1
            yield _parse_error_record(
                item.raw, line_number=line_number, byte_offset=item.byte_offset
            )
            continue
        line_number = record.line_number
        yield record


def content_blocks(record: PiRecord) -> list[dict[str, Any]]:
    message = record.raw_json.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        return [pg_safe(block) for block in content if isinstance(block, dict)]
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


__all__ = [
    "KNOWN_PI_TYPES",
    "PiRecord",
    "content_blocks",
    "iter_records",
    "parse_line",
    "pg_safe",
    "timestamp",
]
