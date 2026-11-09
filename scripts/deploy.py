#!/usr/bin/env python3
"""Deploy a shared checkout into an isolated, machine-local /root directory."""
from __future__ import annotations

import argparse
import fcntl
import getpass
import json
import os
import pwd
import secrets
import shlex
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

SOURCE_ROOT = Path(__file__).resolve().parent.parent
EXCLUDED_PARTS = {
    ".git", ".venv", "venv", "node_modules", "dist", "runtime", "__pycache__",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", "test-results", "playwright-report",
}


def run(command: list[str], *, cwd: Path | None = None, env: dict | None = None,
        input_text: str | None = None, capture: bool = False) -> str:
    result = subprocess.run(
        command, cwd=cwd, env=env, input=input_text, text=True,
        capture_output=capture, check=True,
    )
    return result.stdout.strip() if capture else ""


def local_home(value: str) -> Path:
    home = Path(value).expanduser().resolve()
    if home == Path("/root") or not home.is_relative_to(Path("/root")):
        raise ValueError("AGENT_OBS_HOME must be a dedicated directory under /root")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    return home


def copy_source(root: Path, target: Path) -> None:
    listing = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"],
        cwd=root, capture_output=True, check=True,
    ).stdout
    target.mkdir(parents=True, exist_ok=False)
    states: dict[Path, tuple[int, int, int]] = {}
    for name in sorted(set(listing.split(b"\0")) - {b""}):
        relative = Path(os.fsdecode(name))
        if (any(part in EXCLUDED_PARTS or part.endswith(".egg-info") for part in relative.parts)
                or relative.name == ".env" or relative.name.endswith(".env.local")):
            continue
        source = root / relative
        if not source.exists():
            continue
        if source.is_symlink() and not source.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"refusing external source symlink: {relative}")
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        before = source.stat()
        shutil.copy2(source, destination)
        after = source.stat()
        state = (before.st_ino, before.st_size, before.st_mtime_ns)
        if state != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError(f"source changed during snapshot; retry deployment: {relative}")
        states[relative] = state
    for relative, state in states.items():
        stat = (root / relative).stat()
        if state != (stat.st_ino, stat.st_size, stat.st_mtime_ns):
            raise ValueError(f"source changed during snapshot; retry deployment: {relative}")
    revision = run(["git", "rev-parse", "HEAD"], cwd=root, capture=True)
    (target / "release.json").write_text(json.dumps({
        "revision": revision,
        "built_at": datetime.now(UTC).isoformat(),
        "working_tree_included": True,
    }, indent=2) + "\n")


def read_config(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            parts = shlex.split(value, comments=True)
            values[key.strip()] = parts[0] if parts else ""
    return values


def configure(home: Path) -> dict[str, str]:
    path = home / "config.env"
    if path.exists():
        path.chmod(0o600)
        return read_config(path)
    port = int(os.environ.get("AGENT_OBS_PG_PORT", "55433"))
    password = secrets.token_urlsafe(32)
    values = {
        "AGENT_OBS_HOME": str(home),
        "AGENT_OBS_ENVIRONMENT": "production",
        "AGENT_OBS_HOST": "127.0.0.1",
        "AGENT_OBS_PORT": "8125",
        "AGENT_OBS_DATABASE_URL": f"postgresql+asyncpg://observatory:{password}@127.0.0.1:{port}/agent_observatory",
        "AGENT_OBS_SESSION_SECRET": secrets.token_urlsafe(48),
        "AGENT_OBS_API_TOKEN_SECRET": secrets.token_urlsafe(48),
        "AGENT_OBS_BOOTSTRAP_TOKEN": "",
        "AGENT_OBS_RETENTION_ENABLED": "false",
        "AGENT_OBS_ADMIN_USERNAME": "admin",
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write("".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items()))
    return values


def database_env(config: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    url = urlparse(config["AGENT_OBS_DATABASE_URL"])
    if url.hostname != "127.0.0.1" or url.path != "/agent_observatory" or url.port == 5432:
        raise ValueError("deployment requires the dedicated loopback PostgreSQL database, not the shared 5432 instance")
    environment = os.environ.copy()
    environment["PGPASSWORD"] = unquote(url.password or "")
    connection = ["-h", "127.0.0.1", "-p", str(url.port), "-U", unquote(url.username or "")]
    return environment, connection


def ensure_postgresql(home: Path, config: dict[str, str]) -> Path:
    binaries = Path(os.environ.get("AGENT_OBS_PG_BIN", "/usr/lib/postgresql/16/bin"))
    for executable in ("initdb", "pg_ctl", "psql", "pg_dump"):
        if not (binaries / executable).is_file():
            raise ValueError(f"PostgreSQL 16 executable missing: {binaries / executable}")
    for executable in ("runuser", "setfacl", "uv", "node", "npm"):
        if not shutil.which(executable):
            raise ValueError(f"missing deployment dependency: {executable}")
    if not run(["node", "--version"], capture=True).startswith("v22."):
        raise ValueError("Node 22 is required; put it on PATH")
    database_env(config)
    account = pwd.getpwnam("postgres")
    for parent in [Path("/root"), *reversed(home.parents), home]:
        if parent != Path("/root") and not parent.is_relative_to(Path("/root")):
            continue
        permissions = "u:postgres:--x,g::---,o::---" if parent == home else "u:postgres:--x"
        run(["setfacl", "-m", permissions, str(parent)])
    postgres = home / "postgresql"
    postgres.mkdir(exist_ok=True, mode=0o700)
    os.chown(postgres, account.pw_uid, account.pw_gid)
    postgres.chmod(0o700)
    data, sockets, logs = postgres / "data", postgres / "run", postgres / "logs"
    for directory in (sockets, logs):
        directory.mkdir(exist_ok=True, mode=0o700)
        os.chown(directory, account.pw_uid, account.pw_gid)
    prefix = ["runuser", "-u", "postgres", "--"]
    if not (data / "PG_VERSION").exists():
        if data.exists() and any(data.iterdir()):
            raise ValueError("refusing to initialize a non-empty PostgreSQL directory")
        run([*prefix, str(binaries / "initdb"), "-D", str(data),
             "--auth-local=peer", "--auth-host=scram-sha-256", "--encoding=UTF8"], cwd=postgres)
        port = urlparse(config["AGENT_OBS_DATABASE_URL"]).port
        with (data / "postgresql.conf").open("a") as handle:
            handle.write(
                f"\nlisten_addresses = '127.0.0.1'\nport = {port}\n"
                f"unix_socket_directories = '{sockets}'\n"
                "logging_collector = on\nlog_directory = '../logs'\n"
                "log_filename = 'postgresql-%Y-%m-%d.log'\nlog_rotation_age = '1d'\n"
            )
    status = subprocess.run([*prefix, str(binaries / "pg_ctl"), "-D", str(data), "status"],
                            cwd=postgres, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if status.returncode != 0:
        run([*prefix, str(binaries / "pg_ctl"), "-D", str(data), "-l", str(logs / "startup.log"), "-w", "start"], cwd=postgres)
    url = urlparse(config["AGENT_OBS_DATABASE_URL"])
    psql = [*prefix, str(binaries / "psql"), "-X", "-qAt", "-v", "ON_ERROR_STOP=1",
            "-h", str(sockets), "-p", str(url.port), "-d", "postgres"]
    username, password = unquote(url.username or ""), unquote(url.password or "")
    if username != "observatory":
        raise ValueError("the dedicated deployment database role must be observatory")
    if not run(psql, input_text="SELECT 1 FROM pg_roles WHERE rolname = 'observatory';", capture=True):
        escaped_password = password.replace("'", "''")
        run(psql, input_text=f"CREATE ROLE observatory LOGIN PASSWORD '{escaped_password}';", capture=True)
    for database in ("agent_observatory", "agent_observatory_ci"):
        if not run(psql, input_text=f"SELECT 1 FROM pg_database WHERE datname = '{database}';", capture=True):
            run(psql, input_text=f"CREATE DATABASE {database} OWNER observatory;", capture=True)
    return binaries


def build(root: Path, home: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
    release = home / "releases" / stamp
    print(f"Building machine-local release {release}", flush=True)
    copy_source(root, release)
    environment = os.environ.copy()
    environment.pop("VIRTUAL_ENV", None)
    environment.pop("UV_PROJECT_ENVIRONMENT", None)
    environment["UV_CACHE_DIR"] = str(home / "cache" / "uv")
    environment["PYTHONPYCACHEPREFIX"] = str(home / "cache" / "pycache")
    proxy = os.environ.get("AGENT_OBS_DEPLOY_PROXY") or read_config(home / "config.env").get("AGENT_OBS_DEPLOY_PROXY")
    if proxy:
        environment["npm_config_proxy"] = proxy
        environment["npm_config_https_proxy"] = proxy
    run(["uv", "sync", "--locked"], cwd=release / "backend", env=environment)
    run(["npm", "ci", "--cache", str(home / "cache" / "npm")], cwd=release / "frontend", env=environment)
    run(["npm", "run", "build"], cwd=release / "frontend", env=environment)
    return release


def application_env(home: Path) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("AGENT_OBS_") and key not in {"TEST_DATABASE_URL", "E2E_DATABASE_URL", "PYTHONPATH"}}
    environment["AGENT_OBS_HOME"] = str(home)
    environment["AGENT_OBS_ENV_FILE"] = str(home / "config.env")
    environment["PYTHONPYCACHEPREFIX"] = str(home / "cache" / "pycache")
    return environment


def cli(release: Path, home: Path, arguments: list[str]) -> None:
    run([str(release / "backend" / ".venv" / "bin" / "agent-observatory"), *arguments],
        cwd=release / "backend", env=application_env(home))


def switch_release(home: Path, release: Path) -> None:
    link = home / ".current-next"
    link.unlink(missing_ok=True)
    link.symlink_to(release, target_is_directory=True)
    link.replace(home / "current")


def activate(release: Path, home: Path, config: dict[str, str], binaries: Path) -> None:
    previous = (home / "current").resolve() if (home / "current").is_symlink() else None
    environment = application_env(home)
    python = release / "backend" / ".venv" / "bin" / "python"
    alive = run([str(python), "-c", "from agent_observatory.supervisor import read_supervisor_pid; import os; print(bool(read_supervisor_pid(os.environ['AGENT_OBS_HOME'])))"], env=environment, capture=True) == "True"
    if alive and previous is None:
        raise ValueError("legacy supervisor detected: migrate its database and stop it through the CLI before activation")
    db_environment, connection = database_env(config)
    backup = home / "backups" / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + ".dump")
    backup.parent.mkdir(exist_ok=True, mode=0o700)
    run([str(binaries / "pg_dump"), *connection, "-d", "agent_observatory", "-Fc", "--no-owner", "--no-acl", "-f", str(backup)], env=db_environment)
    backup.chmod(0o600)
    if alive:
        cli(previous, home, ["down"])
    try:
        run([str(python), "-m", "alembic", "upgrade", "head"], cwd=release / "backend", env=environment)
        run([str(python), "-m", "alembic", "check"], cwd=release / "backend", env=environment)
        credentials = read_config(home / "credentials.env")
        password = os.environ.get("AGENT_OBS_ADMIN_PASSWORD") or credentials.get("AGENT_OBS_ADMIN_PASSWORD")
        has_users = run([str(binaries / "psql"), *connection, "-d", "agent_observatory", "-XAtc", "SELECT count(*) FROM users"], env=db_environment, capture=True) != "0"
        if not has_users and not password:
            if not sys.stdin.isatty():
                raise ValueError("set AGENT_OBS_ADMIN_PASSWORD for the initial administrator")
            password = getpass.getpass("Initial admin password: ")
            if password != getpass.getpass("Repeat initial admin password: "):
                raise ValueError("administrator passwords do not match")
        if not has_users:
            environment["AGENT_OBS_ADMIN_USERNAME"] = "admin"
            environment["AGENT_OBS_ADMIN_PASSWORD"] = password
            run([str(python), "-c", "from agent_observatory.cli import _run_async, _ensure_initial_admin; raise SystemExit(_run_async(_ensure_initial_admin()))"], env=environment)
            credential_path = home / "credentials.env"
            descriptor = os.open(credential_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w") as handle:
                handle.write(f"AGENT_OBS_ADMIN_PASSWORD={shlex.quote(password)}\n")
            credential_path.chmod(0o600)
        switch_release(home, release)
        cli(release, home, ["up"])
        cli(release, home, ["status", "--json"])
        record_deployment(release, home, backup)
        (home / "start.sh").write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            f"export AGENT_OBS_HOME={shlex.quote(str(home))}\n"
            f"exec python3 {shlex.quote(str(home / 'current' / 'scripts' / 'deploy.py'))} --start-only\n"
        )
        (home / "start.sh").chmod(0o700)
        print(f"Deployed {release}; backup: {backup}", flush=True)
    except BaseException:
        if previous is not None:
            try:
                current_alive = run([str(python), "-c", "from agent_observatory.supervisor import read_supervisor_pid; import os; print(bool(read_supervisor_pid(os.environ['AGENT_OBS_HOME'])))"], env=application_env(home), capture=True) == "True"
                if current_alive:
                    cli(release, home, ["down"])
                switch_release(home, previous)
                if alive:
                    cli(previous, home, ["up"])
            except (OSError, subprocess.CalledProcessError):
                print("Runtime rollback failed; inspect machine-local logs and backup", file=sys.stderr)
        raise


def record_deployment(release: Path, home: Path, backup: Path) -> None:
    program = """
import asyncio
import json
import sys
from sqlalchemy.ext.asyncio import async_sessionmaker
from agent_observatory.db.session import get_engine, reset_engine
from agent_observatory.services import audit

async def record():
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with factory() as database:
        details = json.loads(sys.argv[1])
        await audit.record(database, action="deployment.applied", actor_type=audit.ACTOR_SYSTEM,
                           resource_type="deployment", resource_id=details["release"], detail=details)
        await database.commit()
    await reset_engine()

asyncio.run(record())
"""
    details = {"release": release.name, "backup_path": str(backup),
               "data_directory": str(home / "postgresql" / "data")}
    run([str(release / "backend" / ".venv" / "bin" / "python"), "-c", program, json.dumps(details)],
        env=application_env(home))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--start-only", action="store_true")
    parser.add_argument("--release", type=Path)
    arguments = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError("this deployment profile must run as root")
    home = local_home(os.environ.get("AGENT_OBS_HOME", "/root/.agent-observatory"))
    (home / "run").mkdir(exist_ok=True)
    with (home / "run" / "deploy.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        config = configure(home)
        binaries = ensure_postgresql(home, config)
        if arguments.start_only:
            cli((home / "current").resolve(strict=True), home, ["up"])
            return 0
        release = arguments.release.resolve() if arguments.release else build(SOURCE_ROOT, home)
        if not release.is_relative_to(home / "releases") or not (release / "backend" / ".venv" / "bin" / "python").is_file():
            raise ValueError("release must be a built machine-local snapshot under AGENT_OBS_HOME/releases")
        if arguments.prepare_only:
            print(f"Prepared release (not activated): {release}", flush=True)
            return 0
        activate(release, home, config, binaries)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        if isinstance(error, subprocess.CalledProcessError):
            print(f"Deployment command failed: {Path(error.cmd[0]).name} (exit {error.returncode})", file=sys.stderr)
        else:
            print(f"Deployment failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
