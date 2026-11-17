from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def deploy_module():
    spec = importlib.util.spec_from_file_location("observatory_deploy", ROOT / "scripts" / "deploy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_machine_config_file_is_used(monkeypatch, tmp_path):
    from agent_observatory.config import settings

    config = tmp_path / "config.env"
    config.write_text("AGENT_OBS_PORT=18325\nAGENT_OBS_PI_HOME=/tmp/synthetic-pi\n")
    monkeypatch.setenv("AGENT_OBS_ENV_FILE", str(config))
    settings.cache_clear()
    assert settings().port == 18325
    assert settings().pi_home == "/tmp/synthetic-pi"
    settings.cache_clear()


def test_shared_checkout_is_copied_without_runtime_or_git(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    (repository / ".gitignore").write_text("runtime/\nnode_modules/\n.env\n")
    (repository / "source.py").write_text("print('synthetic source')\n")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    (repository / "source.py").write_text("print('uncommitted source')\n")
    (repository / "new.py").write_text("print('new source')\n")
    (repository / "runtime").mkdir()
    (repository / "runtime" / "private.txt").write_text("synthetic private output")
    (repository / ".env").write_text("SYNTHETIC_PRIVATE_VALUE=not-for-release")
    module = deploy_module()
    original_run = module.run

    def fake_run(command, **arguments):
        if command == ["git", "rev-parse", "HEAD"]:
            return "synthetic-revision"
        return original_run(command, **arguments)

    module.run = fake_run
    release = tmp_path / "release"
    module.copy_source(repository, release)
    assert (release / "source.py").read_text() == "print('uncommitted source')\n"
    assert (release / "new.py").exists()
    assert not (release / "runtime").exists()
    assert not (release / ".git").exists()
    assert not (release / ".env").exists()


def test_manage_uses_machine_cli_without_shared_dependencies(tmp_path):
    home = tmp_path / "home"
    executable = home / "current" / "backend" / ".venv" / "bin" / "agent-observatory"
    executable.parent.mkdir(parents=True)
    executable.write_text('#!/bin/sh\nprintf "%s\\n" "$AGENT_OBS_ENV_FILE" "$@"\n')
    executable.chmod(0o700)
    environment = os.environ.copy()
    environment.pop("AGENT_OBS_ENV_FILE", None)
    environment["AGENT_OBS_HOME"] = str(home)
    result = subprocess.run(["bash", str(ROOT / "manage.sh"), "status", "--json"],
                            env=environment, text=True, capture_output=True, check=True)
    assert result.stdout.splitlines() == [str(home / "config.env"), "status", "--json"]


def test_deployment_rejects_shared_data_directory(tmp_path):
    with pytest.raises(ValueError, match="under /root"):
        deploy_module().local_home("/mnt/shared/synthetic-observatory")


def test_e2e_refuses_non_ci_before_running_migrations(tmp_path):
    marker = tmp_path / "migration-called"
    migration = tmp_path / "alembic"
    migration.write_text(f"#!/bin/sh\ntouch {marker}\nexit 99\n")
    migration.chmod(0o700)
    environment = os.environ.copy()
    environment.update({
        "PYTHON_BIN": sys.executable,
        "ALEMBIC_BIN": str(migration),
        "E2E_DATABASE_URL": "postgresql+asyncpg://synthetic@127.0.0.1/agent_observatory",
    })
    result = subprocess.run(["bash", str(ROOT / "backend" / "tests" / "run_e2e_server.sh")],
                            env=environment, text=True, capture_output=True)
    assert result.returncode == 1
    assert "refusing E2E migrations" in result.stderr
    assert not marker.exists()


def test_postgres_profile_defaults_overrides_and_validation():
    deploy = deploy_module()
    defaults = deploy.postgres_settings({})
    assert defaults == {
        "shared_buffers": "32MB",
        "max_connections": "100",
        "jit": "off",
        "maintenance_work_mem": "32MB",
        "default_toast_compression": "lz4",
    }
    roomy = deploy.postgres_settings({"AGENT_OBS_PG_SHARED_BUFFERS": "128MB"})
    assert roomy["shared_buffers"] == "128MB"
    assert roomy["jit"] == "off"
    with pytest.raises(ValueError, match="AGENT_OBS_PG_JIT"):
        deploy.postgres_settings({"AGENT_OBS_PG_JIT": "off'; DROP TABLE users; --"})


def test_tune_postgresql_applies_profile_and_reports_restart(monkeypatch, tmp_path):
    deploy = deploy_module()
    inputs: list[str] = []

    def fake_run(command, *, input_text=None, capture=False, **_kwargs):
        assert command[:4] == ["runuser", "-u", "postgres", "--"]
        inputs.append(input_text or "")
        return "t" if "bool_or" in (input_text or "") else ""

    monkeypatch.setattr(deploy, "run", fake_run)
    config = {
        "AGENT_OBS_DATABASE_URL": "postgresql+asyncpg://observatory:secret@127.0.0.1:55433/agent_observatory"
    }
    assert deploy.tune_postgresql(tmp_path, config, Path("/pg/bin")) is True
    assert "ALTER SYSTEM SET shared_buffers = '32MB';" in inputs[0]
    assert "ALTER SYSTEM SET default_toast_compression = 'lz4';" in inputs[0]
    assert "pg_reload_conf()" in inputs[0]
    assert "('max_connections', '100')" in inputs[1]
    assert "secret" not in "".join(inputs)
