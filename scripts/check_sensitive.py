#!/usr/bin/env python3
"""Fail when release content contains externally supplied sensitive terms.

Terms are intentionally never stored in this repository.  Configure either
``SENSITIVE_TERMS`` (comma/newline separated) or ``SENSITIVE_TERMS_FILE``
(one term per line, ``#`` comments allowed).  Matches are reported by term
number rather than by value so CI logs do not repeat the sensitive content.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

EXCLUDED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "coverage",
    "dist",
    "node_modules",
    "playwright-report",
    "runtime",
    "test-results",
    "venv",
}
EXCLUDED_SUFFIXES = {
    ".avif",
    ".db",
    ".db-shm",
    ".db-wal",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".pyc",
    ".sqlite",
    ".sqlite3",
    ".webp",
    ".woff",
    ".woff2",
    ".zip",
    ".zst",
}
MAX_FILE_BYTES = 10 * 1024 * 1024


def _split_terms(value: str) -> list[str]:
    terms: list[str] = []
    for line in value.splitlines():
        for item in line.split(","):
            term = item.strip()
            if term and term not in terms:
                terms.append(term)
    return terms


def load_terms() -> list[str]:
    terms = _split_terms(os.environ.get("SENSITIVE_TERMS", ""))
    terms_file = os.environ.get("SENSITIVE_TERMS_FILE", "").strip()
    if terms_file:
        path = Path(terms_file).expanduser()
        for line in path.read_text(encoding="utf-8").splitlines():
            term = line.strip()
            if not term or term.startswith("#"):
                continue
            if term not in terms:
                terms.append(term)
    return terms


def _iter_tree(path: Path) -> Iterator[Path]:
    if path.is_file():
        yield path
        return
    for child in sorted(path.rglob("*")):
        if not child.is_file():
            continue
        relative_parts = child.relative_to(path).parts
        if any(part in EXCLUDED_DIRS for part in relative_parts):
            continue
        if child.suffix.lower() in EXCLUDED_SUFFIXES:
            continue
        yield child


def _git_files(root: Path) -> list[Path] | None:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard", "-z"],
            cwd=root,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return [
        root / item.decode("utf-8", "surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    ]


def candidate_files(root: Path, paths: Iterable[Path]) -> list[Path]:
    explicit = list(paths)
    if explicit:
        files: list[Path] = []
        for path in explicit:
            files.extend(_iter_tree(path))
        return sorted(set(files))
    tracked = _git_files(root)
    if tracked is not None:
        return sorted(
            path
            for path in tracked
            if path.is_file()
            and not any(part in EXCLUDED_DIRS for part in path.relative_to(root).parts)
            and path.suffix.lower() not in EXCLUDED_SUFFIXES
        )
    return list(_iter_tree(root))


def scan(files: Iterable[Path], terms: list[str]) -> list[tuple[Path, int, int]]:
    normalized = [(index + 1, term.casefold()) for index, term in enumerate(terms)]
    findings: list[tuple[Path, int, int]] = []
    for path in files:
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            payload = path.read_bytes()
        except OSError:
            continue
        if b"\0" in payload[:8192]:
            continue
        text = payload.decode("utf-8", "replace")
        for line_number, line in enumerate(text.splitlines(), start=1):
            folded = line.casefold()
            for term_number, term in normalized:
                if term in folded:
                    findings.append((path, line_number, term_number))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Files or directories to scan; defaults to tracked/non-ignored repo files.",
    )
    args = parser.parse_args(argv)

    try:
        terms = load_terms()
    except OSError as exc:
        print(f"cannot read SENSITIVE_TERMS_FILE: {exc}", file=sys.stderr)
        return 2
    if not terms:
        print(
            "No sensitive terms configured. Set SENSITIVE_TERMS or "
            "SENSITIVE_TERMS_FILE before running the release check.",
            file=sys.stderr,
        )
        return 2

    root = Path.cwd()
    findings = scan(candidate_files(root, args.paths), terms)
    for path, line_number, term_number in findings:
        try:
            display = path.resolve().relative_to(root.resolve())
        except ValueError:
            display = path
        print(f"{display}:{line_number}: matched sensitive term #{term_number}")
    if findings:
        print(f"Sensitive-term check failed: {len(findings)} match(es).", file=sys.stderr)
        return 1
    print(f"Sensitive-term check passed ({len(terms)} term(s) configured).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
