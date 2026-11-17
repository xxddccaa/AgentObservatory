"""Incremental, read-only replacement for ``Path.rglob`` over agent data roots.

Rescanning every root on every 2 s poll used to dominate the indexer's CPU:
``~/.stepcode/codex`` alone holds thousands of temporary directories and
``~/.config/opencode`` a ``node_modules`` tree, none of which ever contain a
session.  :class:`TreeCache` keeps the directory listing in memory and only
re-lists a directory when its mtime changes -- creating, deleting or renaming
an entry always updates the mtime of the directory that holds it.

Fast polls only ``stat`` the directories where new sessions actually appear
(see :meth:`TreeCache._is_hot`); every directory is re-validated at least every
:attr:`TreeCache.VALIDATE_INTERVAL` seconds, and the whole tree is re-listed
from scratch every :attr:`TreeCache.REBUILD_INTERVAL` seconds as a backstop.
Callers outside the polling loop (tests, ``indexer --once``) get the
validating behaviour by default, so their results match ``rglob`` exactly.

Appends to known files are *not* this module's concern: the orchestrator
stats every known source on every poll, which is cheap.
"""
from __future__ import annotations

import fnmatch
import functools
import os
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_FULL_SCAN: ContextVar[bool] = ContextVar("agent_observatory_full_scan", default=True)


def full_scan_requested() -> bool:
    """True unless the polling loop is running a fast (hot-only) cycle."""
    return _FULL_SCAN.get()


@contextmanager
def scan_mode(*, full: bool) -> Iterator[None]:
    """Run discovery in full (validate everything) or fast (hot-only) mode."""
    token = _FULL_SCAN.set(full)
    try:
        yield
    finally:
        _FULL_SCAN.reset(token)


@dataclass(slots=True)
class _Dir:
    mtime_ns: int
    #: The mtime was too close to "now" to prove that no later change shares
    #: the same timestamp tick, so the next check must re-list regardless.
    racy: bool
    subdirs: tuple[str, ...]
    matches: tuple[str, ...]


class TreeCache:
    """``rglob(pattern)`` results for one root, refreshed incrementally.

    Semantics match ``Path.rglob`` on Python 3.12: real sub-directories are
    descended (directory symlinks are not followed, the root itself may be a
    symlink) and every entry whose *name* matches ``pattern`` is returned,
    whatever its type.  Callers keep their own ``is_file`` checks.
    """

    #: Every cached directory is ``stat``-ed at least this often.
    VALIDATE_INTERVAL = 10.0
    #: Drop the cache and re-list everything from scratch this often.
    REBUILD_INTERVAL = 600.0
    #: Directories modified this recently are checked on every fast poll.
    HOT_WINDOW = 600.0
    #: Timestamp granularity margin, see :attr:`_Dir.racy`.
    RACY_WINDOW = 2.0

    def __init__(
        self,
        root: str | Path,
        pattern: str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root)
        self._root = os.fspath(self.root)
        self._match = re.compile(fnmatch.translate(pattern)).match
        self._clock = clock
        self._dirs: dict[str, _Dir] = {}
        self._structural: frozenset[str] = frozenset()
        self._result: list[Path] | None = None
        self._built_at: float | None = None
        self._validated_at = 0.0
        #: Cumulative counters, handy when diagnosing polling cost.
        self.stats = {"rebuilds": 0, "relisted": 0, "checked": 0}

    def glob(self) -> list[Path]:
        now = self._clock()
        if self._built_at is None or now - self._built_at >= self.REBUILD_INTERVAL:
            self._rebuild(now)
        else:
            validate = (
                full_scan_requested()
                or now - self._validated_at >= self.VALIDATE_INTERVAL
            )
            self._refresh(now, validate=validate)
        if self._result is None:
            self._result = [
                Path(match) for entry in self._dirs.values() for match in entry.matches
            ]
        return list(self._result)

    # -- refresh ---------------------------------------------------------

    def _rebuild(self, now: float) -> None:
        self._dirs = {}
        self._scan_tree(self._root, now)
        self._changed()
        self._built_at = now
        self._validated_at = now
        self.stats["rebuilds"] += 1

    def _refresh(self, now: float, *, validate: bool) -> None:
        if self._root not in self._dirs:
            # The root did not exist (yet); creating it is the only change
            # that can make it appear, so probe it on every poll.
            self._scan_tree(self._root, now)
            self._changed()
            if validate:
                self._validated_at = now
            return
        if validate:
            candidates = list(self._dirs)
        else:
            candidates = [
                path for path, entry in self._dirs.items() if self._is_hot(path, entry, now)
            ]
        changed = False
        for path in candidates:
            entry = self._dirs.get(path)
            if entry is None:  # dropped together with an ancestor in this pass
                continue
            self.stats["checked"] += 1
            try:
                stat = os.stat(path)
            except OSError:
                self._drop(path)
                changed = True
                continue
            if entry.racy or stat.st_mtime_ns != entry.mtime_ns:
                changed |= self._relist(path, stat, now)
        if changed:
            self._changed()
        if validate:
            self._validated_at = now

    def _is_hot(self, path: str, entry: _Dir, now: float) -> bool:
        """Where a new session file can appear between validations.

        That is the root, every directory that already holds a match and its
        ancestors (a new dated sub-directory or project folder is created
        there), and anything modified recently (a folder created moments
        ago that does not hold a session yet).
        """
        return (
            entry.racy
            or path in self._structural
            or now - entry.mtime_ns / 1e9 < self.HOT_WINDOW
        )

    def _relist(self, path: str, stat: os.stat_result, now: float) -> bool:
        listing = self._list(path, stat, now)
        if listing is None:
            self._drop(path)
            return True
        old = self._dirs.get(path)
        self._dirs[path] = listing
        self.stats["relisted"] += 1
        if old is not None:
            for gone in set(old.subdirs) - set(listing.subdirs):
                self._drop(gone)
        for sub in listing.subdirs:
            if sub not in self._dirs:
                self._scan_tree(sub, now)
        return old is None or old.subdirs != listing.subdirs or old.matches != listing.matches

    def _scan_tree(self, top: str, now: float) -> None:
        stack = [top]
        while stack:
            path = stack.pop()
            try:
                stat = os.stat(path)
            except OSError:
                continue
            listing = self._list(path, stat, now)
            if listing is None:
                continue
            self._dirs[path] = listing
            self.stats["relisted"] += 1
            stack.extend(sub for sub in listing.subdirs if sub not in self._dirs)

    def _list(self, path: str, stat: os.stat_result, now: float) -> _Dir | None:
        """List one directory.  ``stat`` must be taken *before* listing, so a
        change racing the listing leaves a stale mtime and is re-listed."""
        try:
            with os.scandir(path) as iterator:
                entries = list(iterator)
        except OSError:
            return None
        subdirs: list[str] = []
        matches: list[str] = []
        for entry in entries:
            if self._match(entry.name):
                matches.append(entry.path)
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if is_dir:
                subdirs.append(entry.path)
        return _Dir(
            mtime_ns=stat.st_mtime_ns,
            racy=abs(now - stat.st_mtime_ns / 1e9) < self.RACY_WINDOW,
            subdirs=tuple(subdirs),
            matches=tuple(matches),
        )

    def _drop(self, top: str) -> None:
        stack = [top]
        while stack:
            entry = self._dirs.pop(stack.pop(), None)
            if entry is not None:
                stack.extend(entry.subdirs)

    def _changed(self) -> None:
        self._result = None
        structural = {self._root}
        for path, entry in self._dirs.items():
            if not entry.matches:
                continue
            while path not in structural and path.startswith(self._root):
                structural.add(path)
                parent = os.path.dirname(path)
                if parent == path:
                    break
                path = parent
        self._structural = frozenset(structural)


def rglob_cached(owner: object, root: Path, pattern: str) -> list[Path]:
    """``root.rglob(pattern)`` through a :class:`TreeCache` owned by ``owner``.

    Keyed by root and pattern, so re-pointing an adapter at another root (as
    some tests do) starts a fresh cache instead of serving the old tree.
    """
    caches: dict[tuple[str, str], TreeCache] = vars(owner).setdefault("_tree_caches", {})
    key = (os.fspath(root), pattern)
    tree = caches.get(key)
    if tree is None:
        tree = caches[key] = TreeCache(root, pattern)
    return tree.glob()


#: Symlinks inside agent roots essentially never change, so a resolved path
#: may lag such a change by this long; everything else uses it as-is.
_RESOLVE_TTL = 60.0
_RESOLVE_MAX_ENTRIES = 50_000
_resolved: dict[str, tuple[str, float]] = {}


def ttl_memo[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Memoize a function of path-like arguments for :data:`_RESOLVE_TTL`.

    For values derived from a resolved path (source identities): they may
    lag a symlink change exactly as long as :func:`cached_resolve` does.
    """
    cache: dict[Any, tuple[R, float]] = {}

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        key = (args, tuple(sorted(kwargs.items())))
        now = time.monotonic()
        hit = cache.get(key)
        if hit is not None and now - hit[1] < _RESOLVE_TTL:
            return hit[0]
        value = func(*args, **kwargs)
        if len(cache) >= _RESOLVE_MAX_ENTRIES:
            cache.clear()
        cache[key] = (value, now)
        return value

    return wrapper


def cached_resolve(path: str | Path) -> Path:
    """``Path(path).resolve()`` memoized for :data:`_RESOLVE_TTL` seconds.

    Source identities hash the resolved path, and resolving walks every path
    component with ``lstat``; doing that for each known session on each poll
    cost more than everything else the poll did.
    """
    key = os.fspath(path)
    now = time.monotonic()
    hit = _resolved.get(key)
    if hit is not None and now - hit[1] < _RESOLVE_TTL:
        return Path(hit[0])
    resolved = os.path.realpath(key)
    if len(_resolved) >= _RESOLVE_MAX_ENTRIES:
        _resolved.clear()
    _resolved[key] = (resolved, now)
    return Path(resolved)


__all__ = [
    "TreeCache",
    "cached_resolve",
    "full_scan_requested",
    "rglob_cached",
    "scan_mode",
    "ttl_memo",
]
