from __future__ import annotations

from agent_observatory.workers.indexer import discovery
from agent_observatory.workers.indexer.claude import ClaudeJsonlSource
from agent_observatory.workers.indexer.generic import GenericJsonlSource
from agent_observatory.workers.indexer.registry import build_registry


def test_probe_agents_merges_known_and_extra_roots(tmp_path) -> None:
    claude = tmp_path / "claude"
    generic = tmp_path / "generic"
    claude.mkdir()
    generic.mkdir()

    roots = discovery.probe_agents(
        {
            "claude": [str(claude)],
            "custom": [str(generic)],
        }
    )
    by_pair = {(root.agent, root.path): root for root in roots}

    assert by_pair[("claude", claude)].exists is True
    assert by_pair[("claude", claude)].implemented is True
    assert by_pair[("custom", generic)].exists is True
    assert by_pair[("custom", generic)].implemented is False
    detected = discovery.detected_agents({"custom": [str(generic)]})
    assert "custom" in detected
    assert detected == sorted(detected)


def test_discovery_summary_marks_implemented_agents(tmp_path) -> None:
    codex = tmp_path / "codex"
    codex.mkdir()
    result = discovery.summary({"codex": [str(codex)]})

    assert result["codex"]["exists"] is True
    assert result["codex"]["implemented"] is True
    assert any(root["path"] == str(codex) for root in result["codex"]["roots"])


def test_registry_maps_extra_roots_to_specific_or_generic_adapters(
    tmp_path, monkeypatch
) -> None:
    from agent_observatory.config import Settings

    codex = tmp_path / "codex"
    stepcode = tmp_path / "stepcode"
    claude = tmp_path / "claude"
    custom = tmp_path / "custom"
    for root in (codex / "sessions", stepcode, claude, custom):
        root.mkdir(parents=True)
    monkeypatch.setenv("AGENT_OBS_CODEX_HOME", str(codex))
    monkeypatch.setenv("AGENT_OBS_STEPCODE_HOME", str(stepcode))
    monkeypatch.setenv(
        "AGENT_OBS_EXTRA_ROOTS",
        f"claude:{claude},vendor:{custom}",
    )
    adapters = build_registry(Settings(_env_file=None))

    assert any(
        isinstance(adapter, ClaudeJsonlSource) and adapter.root == claude
        for adapter in adapters
    )
    assert any(
        isinstance(adapter, GenericJsonlSource)
        and adapter.root == custom
        and adapter.agent == "generic"
        for adapter in adapters
    )


def test_registry_dedupes_adapters_reading_the_same_real_directory(tmp_path) -> None:
    """A symlinked root must not index the same files under two agent labels.

    ``~/.stepcode/claude/projects`` is normally a symlink to
    ``~/.claude/projects``; both adapters used to enumerate every Claude
    session, doubling that part of each polling cycle.
    """
    from agent_observatory.workers.indexer.claude import ClaudeJsonlSource
    from agent_observatory.workers.indexer.registry import _deduplicate_adapters

    real = tmp_path / "claude-projects"
    real.mkdir()
    link = tmp_path / "stepcode-claude-projects"
    link.symlink_to(real, target_is_directory=True)
    other = tmp_path / "other-projects"
    other.mkdir()

    deduped = _deduplicate_adapters(
        [
            ClaudeJsonlSource(real, agent="claude", parser_version="2"),
            ClaudeJsonlSource(link, agent="stepcode", parser_version="2"),
            ClaudeJsonlSource(other, agent="claude", parser_version="2"),
        ]
    )
    assert [adapter.agent for adapter in deduped] == ["claude", "claude"]
    assert deduped[0].root == real
    assert deduped[1].root == other


def test_new_native_roots_created_after_registry_start_are_discovered(tmp_path, monkeypatch) -> None:
    from agent_observatory.config import Settings
    from agent_observatory.workers.indexer.registry import _adapter_root

    monkeypatch.setenv("HOME", str(tmp_path))
    for agent in ("CODEX", "STEPCODE", "PI", "XDCODE", "STEPGO"):
        monkeypatch.setenv(f"AGENT_OBS_{agent}_HOME", str(tmp_path / agent.lower()))
    monkeypatch.setenv("AGENT_OBS_EXTRA_ROOTS", f"pi:{tmp_path / 'custom-pi'}")
    adapters = [
        adapter for adapter in build_registry(Settings(_env_file=None))
        if adapter.agent in {"pi", "xdcode", "stepgo"}
    ]
    assert len(adapters) == 6
    assert all(adapter.discover() == [] for adapter in adapters)
    for adapter in adapters:
        root = _adapter_root(adapter)
        root.mkdir(parents=True, exist_ok=True)
        path = root / "rollout-synthetic.jsonl"
        path.write_text("{}\n")
        assert [source.path for source in adapter.discover()] == [path]
