"""Build the read-only adapter registry from known and configured roots."""
from __future__ import annotations

import logging
from pathlib import Path

from agent_observatory.config import Settings

from .adapters import (
    CodexJsonlSource,
    SourceAdapter,
    StepCodeSqliteSource,
    XDCodeJsonlSource,
)
from .claude import ClaudeJsonlSource
from .dsh import DshJsonlSource
from .generic import GenericJsonlSource
from .opencode import OpenCodeJsonSource
from .pi import PiJsonlSource
from .scanner import RolloutScanner
from .stepcli import StepCliNativeSource
from .stepcode import StepCodeImporter
from .stepgo_host import StepGoHostSource

logger = logging.getLogger(__name__)


def _existing(path: str | Path) -> Path | None:
    root = Path(path).expanduser()
    return root if root.exists() else None


def build_registry(
    config: Settings,
    *,
    scanner: RolloutScanner | None = None,
    stepcode: StepCodeImporter | None = None,
) -> list[SourceAdapter]:
    """Instantiate adapters, keeping native roots discoverable before first use."""
    adapters: list[SourceAdapter] = []

    codex_scanner = scanner or RolloutScanner(config.sessions_root)
    adapters.append(CodexJsonlSource(codex_scanner))

    stepcode_importer = stepcode or StepCodeImporter(config.stepcode_root)
    adapters.append(
        StepCodeSqliteSource(
            stepcode_importer, parser_version=codex_scanner.parser_version
        )
    )
    stepcli_root = _existing(config.stepcli_sessions_root)
    if stepcli_root is not None:
        adapters.append(StepCliNativeSource(stepcli_root))

    # Pi has a native default and a StepGo-managed root.  They often resolve
    # to the same directory, so the adapter root is de-duplicated below.
    for raw_root in config.pi_sessions_roots:
        adapters.append(PiJsonlSource(Path(raw_root).expanduser()))

    for raw_root in config.xdcode_sessions_roots:
        adapters.append(
            XDCodeJsonlSource(
                RolloutScanner(
                    Path(raw_root).expanduser(),
                    agent="xdcode",
                    source_kind="xdcode_jsonl",
                    identity_prefix="xdcode",
                )
            )
        )

    adapters.append(StepGoHostSource(Path(config.stepgo_host_root).expanduser()))

    # Keep executable layouts explicit. The broad OpenCode roots support
    # installations that nest ``storage`` one level deeper.
    known: list[tuple[str, str, str]] = [
        ("claude", config.claude_projects_root, "claude"),
        ("stepcode", "~/.stepcode/claude/projects", "stepcode"),
        ("dsh", "~/.dsh/sessions", "dsh"),
        ("opencode", "~/.local/share/opencode", "opencode"),
        ("opencode", "~/.config/opencode", "opencode"),
    ]
    for agent, raw_root, adapter_type in known:
        root = _existing(raw_root)
        if root is None:
            continue
        if adapter_type == "claude" or adapter_type == "stepcode":
            adapters.append(ClaudeJsonlSource(root, agent=agent, parser_version="2"))
        elif adapter_type == "dsh":
            adapters.append(DshJsonlSource(root))
        else:
            adapters.append(OpenCodeJsonSource(root))

    for agent, roots in config.extra_roots.items():
        for raw_root in roots:
            root = (
                Path(raw_root).expanduser()
                if agent in {"pi", "xdcode", "stepgo"}
                else _existing(raw_root)
            )
            if root is None:
                continue
            if agent == "codex":
                adapters.append(CodexJsonlSource(RolloutScanner(root)))
            elif agent == "xdcode":
                adapters.append(
                    XDCodeJsonlSource(
                        RolloutScanner(
                            root,
                            agent="xdcode",
                            source_kind="xdcode_jsonl",
                            identity_prefix="xdcode",
                        )
                    )
                )
            elif agent == "pi":
                adapters.append(PiJsonlSource(root))
            elif agent == "stepgo":
                adapters.append(StepGoHostSource(root))
            elif agent == "claude":
                adapters.append(ClaudeJsonlSource(root, agent="claude", parser_version="2"))
            elif agent == "stepcode":
                adapters.append(ClaudeJsonlSource(root, agent="stepcode", parser_version="2"))
            elif agent == "dsh":
                adapters.append(DshJsonlSource(root))
            elif agent == "opencode":
                adapters.append(OpenCodeJsonSource(root))
            elif agent not in {"dsh", "opencode"}:
                adapters.append(GenericJsonlSource(root, agent="generic"))

    return _deduplicate_adapters(adapters)


def _adapter_root(adapter: SourceAdapter) -> Path:
    """Return the directory an adapter reads (scanner/importer roots incl.)."""
    raw = getattr(
        adapter,
        "root",
        getattr(
            getattr(adapter, "scanner", None),
            "root",
            getattr(getattr(adapter, "importer", None), "root", ""),
        ),
    )
    return Path(raw).expanduser()


def _deduplicate_adapters(adapters: list[SourceAdapter]) -> list[SourceAdapter]:
    """Drop adapters that would index the same real directory twice.

    Known layouts overlap on this machine: ``~/.stepcode/claude/projects`` is
    commonly a symlink to ``~/.claude/projects``, which used to index every
    Claude session twice (once per agent label) on every poll.  Roots are
    therefore compared after resolving symlinks, and the first registered
    adapter wins.  Adapters of different classes are both kept, because the
    same directory may legitimately hold two different file formats.
    """
    result: list[SourceAdapter] = []
    seen: set[tuple[str, str]] = set()
    for adapter in adapters:
        root_path = _adapter_root(adapter)
        try:
            root = str(root_path.resolve(strict=False))
        except OSError:
            root = str(root_path)
        key = (_adapter_family(adapter), root)
        if key in seen:
            logger.info(
                "skipping duplicate %s adapter for agent %s (root %s resolves to %s)",
                adapter.kind,
                adapter.agent,
                root_path,
                root,
            )
            continue
        seen.add(key)
        result.append(adapter)
    return result


def _adapter_family(adapter: SourceAdapter) -> str:
    """Filesystem format family used to prevent cross-label duplicate reads."""
    if isinstance(adapter, (CodexJsonlSource, XDCodeJsonlSource)):
        return "codex_jsonl"
    if isinstance(adapter, PiJsonlSource):
        return "pi_jsonl"
    if isinstance(adapter, StepGoHostSource):
        return "stepgo_host_jsonl"
    if isinstance(adapter, ClaudeJsonlSource):
        return "claude_jsonl"
    return type(adapter).__name__


__all__ = ["build_registry"]
