from __future__ import annotations

import os
import sys
import time

import pytest

from agent_observatory.cli import main
from agent_observatory.supervisor import ProcessSpec, Supervisor, pid_matches


def _sleep_spec(name: str = "api", seconds: float = 10) -> dict[str, ProcessSpec]:
    return {
        name: ProcessSpec(
            name,
            [
                sys.executable,
                "-c",
                (
                    "import sys,time; print('hello from child', flush=True); "
                    f"time.sleep({seconds})"
                ),
            ],
        )
    }


def test_help_lists_lifecycle_commands(capsys):
    assert main(["--help"]) == 0
    output = capsys.readouterr().out
    for command in ("up", "down", "restart", "status", "logs", "doctor", "dev"):
        assert command in output


def test_pid_identity_rejects_reused_token():
    pid = os.getpid()
    assert pid_matches(pid, str(_current_start_token() + 1)) is False


def _current_start_token() -> int:
    with open(f"/proc/{os.getpid()}/stat") as handle:
        fields = handle.read().split()
    return int(fields[21])


def test_supervisor_start_status_stop_and_logs(tmp_path):
    supervisor = Supervisor(tmp_path, specs=_sleep_spec(), poll_interval=0.01)
    started = supervisor.start()
    pid = started["components"]["api"]["pid"]
    assert started["components"]["api"]["status"] == "running"
    assert isinstance(pid, int)
    time.sleep(0.05)
    logs = supervisor.tail_logs("api", lines=10)
    assert any("hello from child" in line for line in logs)
    stopped = supervisor.stop()
    assert stopped["components"]["api"]["status"] == "stopped"
    assert not supervisor.pid_path.exists()


def test_supervisor_records_backoff_after_child_exit(tmp_path):
    specs = {
        "api": ProcessSpec(
            "api",
            [sys.executable, "-c", "print('boom', flush=True)"],
        )
    }
    supervisor = Supervisor(tmp_path, specs=specs, poll_interval=0.01)
    supervisor.start()
    deadline = time.time() + 2
    state = {}
    while time.time() < deadline:
        time.sleep(0.02)
        state = supervisor.monitor_once()
        if state["components"]["api"]["status"] in {"backoff", "crash-loop"}:
            break
    assert state["components"]["api"]["status"] in {"backoff", "crash-loop"}
    assert state["components"]["api"]["next_restart_at"] is not None
    supervisor.stop()


def test_stale_pid_does_not_get_signalled(tmp_path):
    supervisor = Supervisor(tmp_path, specs=_sleep_spec())
    supervisor.runtime_dir.mkdir(parents=True, exist_ok=True)
    supervisor._write_state(
        {
            "components": {
                "api": {
                    "pid": os.getpid(),
                    "pid_start_token": "not-this-process",
                    "status": "running",
                }
            }
        }
    )
    status = supervisor.status()
    assert status["components"]["api"]["status"] == "stale"


def test_doctor_is_non_destructive(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OBS_HOME", str(tmp_path))
    supervisor = Supervisor(tmp_path, specs={})
    result = supervisor.doctor()
    assert "checks" in result
    assert result["checks"]["runtime_dir"]["ok"] is True
    assert (tmp_path / "run").exists()


def test_supervisor_lock_rejects_second_instance(tmp_path):
    first = Supervisor(tmp_path, specs={})
    second = Supervisor(tmp_path, specs={})
    first._acquire_lock()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            second._acquire_lock()
    finally:
        first._release_lock()


def test_new_supervisor_replaces_orphaned_child(tmp_path):
    first = Supervisor(tmp_path, specs=_sleep_spec())
    first_status = first.start()
    first_pid = first_status["components"]["api"]["pid"]
    assert isinstance(first_pid, int)

    replacement = Supervisor(tmp_path, specs=_sleep_spec())
    replacement_status = replacement.start()
    replacement_pid = replacement_status["components"]["api"]["pid"]
    assert isinstance(replacement_pid, int)
    assert replacement_pid != first_pid
    deadline = time.time() + 2
    while time.time() < deadline and pid_matches(first_pid, None):
        time.sleep(0.02)
    assert not pid_matches(first_pid, None)
    replacement.stop()


def test_status_does_not_rewrite_state(tmp_path):
    supervisor = Supervisor(tmp_path, specs={})
    supervisor._write_state({"components": {}, "marker": "keep"})
    before = supervisor.state_path.read_bytes()
    supervisor.status()
    assert supervisor.state_path.read_bytes() == before
