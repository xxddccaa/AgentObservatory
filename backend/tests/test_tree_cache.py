"""Incremental directory cache used instead of ``Path.rglob`` while polling."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from agent_observatory.workers.indexer import tree_cache
from agent_observatory.workers.indexer.tree_cache import (
    TreeCache,
    cached_resolve,
    rglob_cached,
    scan_mode,
)

OLD = time.time() - 7200  # comfortably outside the hot and racy windows


class _Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


def _touch(path: Path, *, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _age(*directories: Path) -> None:
    for directory in directories:
        os.utime(directory, (OLD, OLD))


def _names(paths: list[Path], root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in paths}


def test_results_match_rglob(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _touch(root / "a.jsonl")
    _touch(root / "2026" / "11" / "17" / "b.jsonl")
    _touch(root / ".hidden" / "c.jsonl")
    _touch(root / "skip.txt")
    (root / "dir.jsonl").mkdir()  # rglob matches names, whatever the type
    outside = tmp_path / "outside"
    _touch(outside / "d.jsonl")
    (root / "linked").symlink_to(outside, target_is_directory=True)

    expected = _names(list(root.rglob("*.jsonl")), root)
    assert _names(TreeCache(root, "*.jsonl").glob(), root) == expected
    assert "linked/d.jsonl" not in expected  # directory symlinks are not followed


def test_symlinked_root_is_followed(tmp_path: Path) -> None:
    real = tmp_path / "real"
    _touch(real / "x" / "a.jsonl")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert _names(TreeCache(link, "*.jsonl").glob(), link) == {"x/a.jsonl"}


def test_new_and_deleted_files_are_seen_by_validating_scans(tmp_path: Path) -> None:
    root = tmp_path / "root"
    first = _touch(root / "day" / "a.jsonl")
    cache = TreeCache(root, "*.jsonl")
    assert _names(cache.glob(), root) == {"day/a.jsonl"}

    _touch(root / "day" / "b.jsonl")
    _touch(root / "new" / "deep" / "c.jsonl")
    assert _names(cache.glob(), root) == {"day/a.jsonl", "day/b.jsonl", "new/deep/c.jsonl"}

    first.unlink()
    assert _names(cache.glob(), root) == {"day/b.jsonl", "new/deep/c.jsonl"}


def test_fast_scans_find_new_sessions_where_they_appear(tmp_path: Path) -> None:
    """New dated folders under a known ancestor and new files next to known
    ones are found on the very next fast poll."""
    root = tmp_path / "sessions"
    _touch(root / "2026" / "11" / "16" / "rollout-a.jsonl", mtime=OLD)
    _age(root / "2026" / "11" / "16", root / "2026" / "11", root / "2026", root)
    clock = _Clock()
    cache = TreeCache(root, "rollout-*.jsonl", clock=clock)
    cache.glob()

    clock.now += 2
    _touch(root / "2026" / "11" / "16" / "rollout-b.jsonl")
    _touch(root / "2026" / "11" / "17" / "rollout-c.jsonl")
    with scan_mode(full=False):
        found = _names(cache.glob(), root)
    assert found == {
        "2026/11/16/rollout-a.jsonl",
        "2026/11/16/rollout-b.jsonl",
        "2026/11/17/rollout-c.jsonl",
    }


def test_fast_scans_follow_freshly_created_folders(tmp_path: Path) -> None:
    """A folder created moments ago stays hot until it holds a session."""
    root = tmp_path / "projects"
    _touch(root / "project" / "s1.jsonl", mtime=OLD)
    _age(root / "project", root)
    # Real clock: the folder and its first file are created within one mtime
    # tick here, which only the racy-window check can tell apart.
    cache = TreeCache(root, "*.jsonl")
    cache.glob()

    (root / "project" / "s2" / "subagents").mkdir(parents=True)
    with scan_mode(full=False):
        assert _names(cache.glob(), root) == {"project/s1.jsonl"}

    _touch(root / "project" / "s2" / "subagents" / "agent-1.jsonl")
    with scan_mode(full=False):
        assert "project/s2/subagents/agent-1.jsonl" in _names(cache.glob(), root)


def test_cold_directories_wait_for_the_next_validation(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _touch(root / "hot" / "a.jsonl", mtime=OLD)
    for index in range(50):
        (root / ".tmp" / f"scratch-{index}").mkdir(parents=True)
    _age(root / "hot", root / ".tmp", root, *(root / ".tmp").iterdir())
    clock = _Clock()
    cache = TreeCache(root, "*.jsonl", clock=clock)
    cache.glob()

    checked = cache.stats["checked"]
    clock.now += 2
    with scan_mode(full=False):
        cache.glob()
    # Only the root and the folder holding a session, not the 51 cold ones.
    assert cache.stats["checked"] - checked == 2

    cold = root / ".tmp" / "scratch-7"
    _touch(cold / "late.jsonl")
    # Still long ago, so the folder stays cold; only validation re-reads it.
    os.utime(cold, (OLD + 60, OLD + 60))
    clock.now += 2
    with scan_mode(full=False):
        assert ".tmp/scratch-7/late.jsonl" not in _names(cache.glob(), root)
    clock.now += TreeCache.VALIDATE_INTERVAL
    with scan_mode(full=False):
        assert ".tmp/scratch-7/late.jsonl" in _names(cache.glob(), root)


def test_racy_directory_is_relisted_until_its_mtime_settles(tmp_path: Path) -> None:
    """A change in the same timestamp tick as the last listing must not hide."""
    root = tmp_path / "root"
    _touch(root / "a.jsonl")
    stamp = root.stat().st_mtime_ns
    clock = _Clock()
    cache = TreeCache(root, "*.jsonl", clock=clock)
    assert _names(cache.glob(), root) == {"a.jsonl"}

    _touch(root / "b.jsonl")
    os.utime(root, ns=(stamp, stamp))  # same tick: the mtime did not move
    with scan_mode(full=False):
        assert _names(cache.glob(), root) == {"a.jsonl", "b.jsonl"}


def test_missing_root_appears_later(tmp_path: Path) -> None:
    root = tmp_path / "later"
    cache = TreeCache(root, "*.jsonl")
    with scan_mode(full=False):
        assert cache.glob() == []
    _touch(root / "a.jsonl")
    with scan_mode(full=False):
        assert _names(cache.glob(), root) == {"a.jsonl"}


def test_periodic_rebuild_relists_everything(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _touch(root / "cold" / "a.txt", mtime=OLD)
    _age(root / "cold", root)
    clock = _Clock()
    cache = TreeCache(root, "*.jsonl", clock=clock)
    cache.glob()
    # Even a change that left no mtime trace is found by the rebuild.
    _touch(root / "cold" / "b.jsonl")
    _age(root / "cold")
    clock.now += TreeCache.REBUILD_INTERVAL
    with scan_mode(full=False):
        assert _names(cache.glob(), root) == {"cold/b.jsonl"}
    assert cache.stats["rebuilds"] == 2


def test_rglob_cached_follows_root_changes(tmp_path: Path) -> None:
    class Owner:
        pass

    owner = Owner()
    _touch(tmp_path / "one" / "a.jsonl")
    _touch(tmp_path / "two" / "b.jsonl")
    assert [p.name for p in rglob_cached(owner, tmp_path / "one", "*.jsonl")] == ["a.jsonl"]
    assert [p.name for p in rglob_cached(owner, tmp_path / "two", "*.jsonl")] == ["b.jsonl"]


def test_cached_resolve_matches_resolve_and_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    link = tmp_path / "link"
    link.symlink_to(first, target_is_directory=True)
    assert cached_resolve(link / "f") == (link / "f").resolve()

    link.unlink()
    link.symlink_to(second, target_is_directory=True)
    assert cached_resolve(link / "f") == first.resolve() / "f"  # within the TTL
    monkeypatch.setattr(tree_cache, "_RESOLVE_TTL", 0.0)
    assert cached_resolve(link / "f") == second.resolve() / "f"
