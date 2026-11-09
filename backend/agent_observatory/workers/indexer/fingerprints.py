"""Bounded checks for replacement and truncate/rewrite of indexed JSONL files."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

_WINDOW_BYTES = 4096


def file_fingerprint(path: Path) -> dict[str, Any]:
    stat = path.stat()
    window = min(stat.st_size, _WINDOW_BYTES)
    tail_offset = stat.st_size - window
    with path.open("rb") as handle:
        head = handle.read(window)
        handle.seek(tail_offset)
        tail = handle.read(window)
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "path": str(path.resolve()),
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "head_length": len(head),
        "head_hash": hashlib.sha256(head).hexdigest(),
        "tail_offset": tail_offset,
        "tail_length": len(tail),
        "tail_hash": hashlib.sha256(tail).hexdigest(),
    }


def indexed_content_matches(path: Path, previous: dict[str, Any]) -> bool:
    stat = path.stat()
    if previous.get("inode") is not None and (
        previous["inode"] != stat.st_ino or previous.get("device") != stat.st_dev
    ):
        return False
    if "head_hash" not in previous:
        return True
    with path.open("rb") as handle:
        head = handle.read(int(previous["head_length"]))
        handle.seek(int(previous["tail_offset"]))
        tail = handle.read(int(previous["tail_length"]))
    return (
        hashlib.sha256(head).hexdigest() == previous["head_hash"]
        and hashlib.sha256(tail).hexdigest() == previous["tail_hash"]
    )
