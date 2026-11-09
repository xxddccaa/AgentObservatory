from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_sensitive.py"


def _run(args: list[str], **env: str) -> subprocess.CompletedProcess[str]:
    process_env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"SENSITIVE_TERMS", "SENSITIVE_TERMS_FILE"}
    }
    process_env.update(env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        env=process_env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_requires_terms_to_be_configured() -> None:
    result = _run([])
    assert result.returncode == 2
    assert "No sensitive terms configured" in result.stderr


def test_reports_match_without_printing_term(tmp_path: Path) -> None:
    sentinel = "SYNTHETIC_RELEASE_SENTINEL_7f3a"
    target = tmp_path / "sample.txt"
    target.write_text(f"before {sentinel.lower()} after\n", encoding="utf-8")

    result = _run([str(target)], SENSITIVE_TERMS=sentinel)

    assert result.returncode == 1
    assert "matched sensitive term #1" in result.stdout
    assert sentinel not in result.stdout
    assert sentinel not in result.stderr


def test_terms_file_and_clean_tree_pass(tmp_path: Path) -> None:
    target = tmp_path / "clean.txt"
    target.write_text("nothing sensitive here\n", encoding="utf-8")
    terms_file = tmp_path / "terms.txt"
    terms_file.write_text("# supplied outside the repository\nSYNTHETIC_RELEASE_SENTINEL_7f3a\n", encoding="utf-8")

    result = _run(
        [str(target)],
        SENSITIVE_TERMS_FILE=str(terms_file),
    )

    assert result.returncode == 0, result.stderr
    assert "Sensitive-term check passed" in result.stdout
