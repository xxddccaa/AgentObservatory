"""Bounded checks for replacement and truncate/rewrite of indexed JSONL files."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

_WINDOW_BYTES = 4096
_SQLITE_MAGIC = b"SQLite format 3\x00"


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


def sqlite_change_counter(path: Path) -> int | None:
    """Read a SQLite header's change counter without opening a connection.

    Rollback-journal databases bump it on every write transaction, which
    catches in-place updates that keep the size and land within one coarse
    mtime tick.  WAL-mode writes land in ``-wal`` instead.  Opening a real
    connection is what we avoid: even a read-only one rewrites ``-shm``.
    """
    try:
        with path.open("rb") as handle:
            header = handle.read(28)
    except OSError:
        return None
    if len(header) < 28 or not header.startswith(_SQLITE_MAGIC):
        return None
    return int.from_bytes(header[24:28], "big")


def sqlite_signature(path: Path) -> tuple[Any, ...]:
    """Cheap change signature for a SQLite store: main file, WAL, header.

    The ``-shm`` wal-index is deliberately excluded: every reader rewrites
    it, so including it made idle stores look changed on every poll.
    Raises ``OSError`` when the main file is gone.
    """
    stat = path.stat()
    try:
        wal = Path(f"{path}-wal").stat()
        wal_part: tuple[int, int, int] | None = (wal.st_size, wal.st_mtime_ns, wal.st_ino)
    except OSError:
        wal_part = None
    return (
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ino,
        stat.st_dev,
        wal_part,
        sqlite_change_counter(path),
    )


def sqlite_signature_is_racy(
    signature: tuple[Any, ...], now: float, window: float = 2.0
) -> bool:
    """True when the main file or WAL changed within ``window`` seconds of
    ``now``: a later write in the same mtime tick could hide behind it."""
    mtimes: list[int] = [signature[1]]
    wal = signature[4]
    if wal is not None:
        mtimes.append(wal[1])
    return any(abs(now - mtime / 1e9) < window for mtime in mtimes)
