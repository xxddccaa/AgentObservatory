"""Command line entry point for local development and operations."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from agent_observatory import __version__


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("--version", "-V", "version"):
        print(f"agent-observatory {__version__}")
        return 0
    if argv[0] in ("-h", "--help", "help"):
        print(
            "Usage: agent-observatory [command]\n"
            "\n"
            "Commands:\n"
            "  version      Print the version and exit\n"
            "  --help       Show this help\n"
            "\n"
            "  init         Validate local dependencies and configuration\n"
            "  up           Start API and worker under the local supervisor\n"
            "  dev          Start API, worker and Vite in the foreground\n"
            "  down         Stop the local supervisor and all children\n"
            "  restart      Restart all or one component (api|worker)\n"
            "  status       Show component status (--json supported)\n"
            "  logs         Show component logs (--follow supported)\n"
            "  doctor       Check dependencies and configuration\n"
            "  user-add     Provision a user (username --role ... --password ...)\n"
            "  user-passwd  Reset a user's password without the current one\n"
            "  indexer      Discover and index local agent sessions\n"
            "  worker       Run the background indexing worker\n"
        )
        return 0
    if argv[0] == "init":
        return _run_lifecycle("init", argv[1:])
    if argv[0] in {"up", "down", "restart", "status", "logs", "doctor", "dev"}:
        return _run_lifecycle(argv[0], argv[1:])
    if argv[0] in ("user-add", "useradd"):
        return _run_async(_run_user_add(argv[1:]))
    if argv[0] in ("user-passwd", "userpasswd"):
        return _run_async(_run_user_passwd(argv[1:]))
    if argv[0] == "indexer":
        return _run_async(_run_indexer(once="--once" in argv[1:]))
    if argv[0] == "worker":
        return _run_async(_run_worker())
    print(f"agent-observatory: unknown command {argv[0]!r}", file=sys.stderr)
    return 2


def _run_lifecycle(command: str, args: list[str]) -> int:
    """Run an operations command without requiring an already-running API."""
    from agent_observatory.config import settings
    from agent_observatory.supervisor import Supervisor

    parser = argparse.ArgumentParser(prog=f"agent-observatory {command}")
    if command in {"restart", "down"}:
        parser.add_argument("component", nargs="?", choices=["api", "worker", "all"])
    if command == "status":
        parser.add_argument("--json", action="store_true", dest="as_json")
    if command == "doctor":
        parser.add_argument("--json", action="store_true", dest="as_json")
    if command == "logs":
        parser.add_argument("component", nargs="?", default="all")
        parser.add_argument("--lines", type=int, default=100)
        parser.add_argument("--follow", action="store_true")
    ns = parser.parse_args(args)

    home = settings().home
    if command == "init":
        return _initialize_project()

    if command == "doctor":
        result = Supervisor(home).doctor()
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1

    if command == "status":
        result = Supervisor(home).status()
        result["readiness"] = _readiness()
        if ns.as_json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(f"supervisor_pid={result.get('supervisor_pid')}")
            for name, info in result.get("components", {}).items():
                print(f"{name}: {info.get('status', 'unknown')} pid={info.get('pid')}")
        return 0

    if command == "logs":
        supervisor = Supervisor(home)
        if ns.follow:
            try:
                for line in supervisor.follow_logs(ns.component):
                    print(line, flush=True)
            except KeyboardInterrupt:
                return 130
        else:
            for line in supervisor.tail_logs(ns.component, ns.lines):
                print(line)
        return 0

    component = getattr(ns, "component", None)
    if component == "all":
        component = None
    if command in {"down", "restart"}:
        from agent_observatory.supervisor import read_supervisor_pid

        if not read_supervisor_pid(home):
            print("supervisor is not running", file=sys.stderr)
            return 1
    if command in {"up", "dev"} and _prepare_runtime() != 0:
        return 1
    command_type = command if command != "dev" else "up"
    command_id = _enqueue_control(command_type, component, home)
    if command_id is None:
        return 1

    if command == "up":
        return _start_daemon(home, command_id)
    if command == "dev":
        return _run_dev(home, command_id)
    if command == "down":
        return _stop_daemon(home, component, command_id)
    if command == "restart":
        return _restart_daemon(home, component, command_id)
    return 0


def _enqueue_control(command: str, component: str | None, home: str) -> int | None:
    """Persist a SystemCommand when possible; local wakeup is notification only."""
    from agent_observatory.supervisor import enqueue_system_command

    try:
        command_id = _run_async(
            enqueue_system_command(command, component, home=home)
        )
        print(f"queued system command {command_id}")
        wakeup = Path(home).expanduser() / "run" / "supervisor.wakeup"
        wakeup.parent.mkdir(parents=True, exist_ok=True)
        wakeup.touch()
        return command_id
    except Exception as exc:  # noqa: BLE001 - doctor/status remain usable offline
        print(f"could not persist system command: {exc}", file=sys.stderr)
        return None


def _start_daemon(home: str, command_id: int) -> int:
    from agent_observatory.config import settings
    from agent_observatory.supervisor import read_supervisor_pid

    existing = read_supervisor_pid(home)
    if existing:
        print(f"supervisor already running (pid={existing[0]})")
        return _wait_for_up(command_id, home)
    runtime = Path(home).expanduser() / "run"
    logs = Path(home).expanduser() / "logs"
    runtime.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [sys.executable, "-m", "agent_observatory.supervisor", "--run"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        close_fds=True,
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        command_status = _system_command_status(command_id)
        if command_status == "failed":
            print(f"system command {command_id} failed", file=sys.stderr)
            return 1
        if (
            read_supervisor_pid(home)
            and command_status == "applied"
            and _api_ready(home)
        ):
            config = settings()
            print(
                f"supervisor started "
                f"(pid={process.pid}, api=http://{config.host}:{config.port})"
            )
            return 0
        if process.poll() is not None:
            print("supervisor failed to start; inspect logs", file=sys.stderr)
            return 1
        time.sleep(0.1)
    print("supervisor start timed out; inspect status/logs", file=sys.stderr)
    return 1


def _system_command_status(command_id: int) -> str | None:
    from agent_observatory.supervisor import system_command_status

    return _run_async(system_command_status(command_id))


def _wait_for_up(command_id: int, home: str, timeout: float = 15.0) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = _system_command_status(command_id)
        if status == "failed":
            print(f"system command {command_id} failed", file=sys.stderr)
            return 1
        if status == "applied" and _api_ready(home):
            print(f"system command {command_id} applied; API and workers are ready")
            return 0
        time.sleep(0.1)
    print(f"system command {command_id} or readiness timed out", file=sys.stderr)
    return 1


def _api_ready(home: str | None = None) -> bool:
    readiness = _readiness()
    ready = bool(
        readiness
        and readiness.get("status") == "ok"
        and all(readiness.get("workers", {}).values())
    )
    if not ready or home is None or readiness is None:
        return ready
    from agent_observatory.supervisor import Supervisor

    worker_pid = (
        Supervisor(home).status().get("components", {}).get("worker", {}).get("pid")
    )
    heartbeat_pids = {
        readiness.get("components", {}).get(kind, {}).get("pid")
        for kind in ("indexer",)
    }
    return isinstance(worker_pid, int) and heartbeat_pids == {worker_pid}


def _readiness() -> dict[str, Any] | None:
    from agent_observatory.config import settings

    config = settings()
    try:
        with urllib.request.urlopen(
            f"http://{config.host}:{config.port}/api/health/ready", timeout=0.75
        ) as response:
            payload = json.load(response)
            return payload if isinstance(payload, dict) else None
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        json.JSONDecodeError,
    ):
        return None


def _stop_daemon(home: str, component: str | None, command_id: int) -> int:
    from agent_observatory.supervisor import read_supervisor_pid

    if not read_supervisor_pid(home):
        print("supervisor is not running")
        return 1
    return _wait_for_command(command_id, home, expect_supervisor_exit=component is None)


def _restart_daemon(home: str, component: str | None, command_id: int) -> int:
    from agent_observatory.supervisor import read_supervisor_pid

    if not read_supervisor_pid(home):
        print("supervisor is not running", file=sys.stderr)
        return 1
    return _wait_for_command(command_id, home)


def _run_dev(home: str, command_id: int) -> int:
    from agent_observatory.supervisor import Supervisor, dev_specs

    supervisor = Supervisor(home, specs=dev_specs())
    # Development mode is foreground-only, so the command is applied by this
    # supervisor instance before it enters the loop.
    supervisor.process_commands_once()
    try:
        supervisor.run_forever()
    except KeyboardInterrupt:
        supervisor.request_stop()
        return 130
    return 0


def _project_paths() -> tuple[Path, Path, Path]:
    root = Path(__file__).resolve().parents[2]
    return root, root / "backend", root / "frontend"


def _npm() -> str | None:
    return shutil.which("npm") or (
        "/opt/node22/bin/npm" if Path("/opt/node22/bin/npm").is_file() else None
    )


def _run_checked(command: list[str], *, cwd: Path, label: str) -> bool:
    print(f"{label}...")
    result = subprocess.run(command, cwd=cwd, check=False)
    if result.returncode != 0:
        print(f"{label} failed (exit {result.returncode})", file=sys.stderr)
        return False
    return True


def _frontend_stale(frontend: Path) -> bool:
    index = frontend / "dist" / "index.html"
    if not index.is_file():
        return True
    built_at = index.stat().st_mtime_ns
    inputs = [frontend / "package.json", frontend / "package-lock.json", frontend / "vite.config.ts"]
    inputs.extend((frontend / "src").rglob("*"))
    return any(path.is_file() and path.stat().st_mtime_ns > built_at for path in inputs)


def _prepare_runtime() -> int:
    _, backend, frontend = _project_paths()
    if not _run_checked(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=backend,
        label="Applying database migrations",
    ):
        return 1
    if _frontend_stale(frontend):
        npm = _npm()
        if npm is None:
            print("npm (Node 22) is required to build the frontend.", file=sys.stderr)
            return 1
        if not (frontend / "node_modules").is_dir() and not _run_checked(
            [npm, "ci"], cwd=frontend, label="Installing frontend dependencies"
        ):
            return 1
        if not _run_checked(
            [npm, "run", "build"], cwd=frontend, label="Building frontend"
        ):
            return 1
    return 0


def _initialize_project() -> int:
    root, backend, _ = _project_paths()
    from agent_observatory.config import settings

    env_path = Path(os.environ.get("AGENT_OBS_ENV_FILE", str(Path(settings().home) / "config.env")))
    os.environ["AGENT_OBS_ENV_FILE"] = str(env_path)
    if not env_path.exists():
        example = root / ".env.example"
        if not example.is_file():
            print(".env.example is missing", file=sys.stderr)
            return 1
        content = example.read_text()
        generated = iter((secrets.token_urlsafe(48), secrets.token_urlsafe(48)))
        marker = "dev-only-change-me-use-random-64-chars"
        content = content.replace(marker, next(generated), 1)
        content = content.replace(marker, next(generated), 1)
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(content)
        os.chmod(env_path, 0o600)
        print(f"created {env_path}")
        settings.cache_clear()
    else:
        os.chmod(env_path, 0o600)
    settings.cache_clear()
    if _prepare_runtime() != 0:
        return 1
    result = _run_async(_ensure_initial_admin())
    if result != 0:
        return result
    from agent_observatory.config import settings
    from agent_observatory.supervisor import Supervisor

    doctor = Supervisor(settings().home).doctor()
    print(json.dumps(doctor, indent=2))
    return 0 if doctor["ok"] else 1


async def _ensure_initial_admin() -> int:
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from agent_observatory.db.models import User
    from agent_observatory.db.session import get_engine
    from agent_observatory.services import audit, users

    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with factory() as db:
        if int(await db.scalar(select(func.count()).select_from(User)) or 0) > 0:
            return 0
        username = os.environ.get("AGENT_OBS_ADMIN_USERNAME", "admin").strip() or "admin"
        password = os.environ.get("AGENT_OBS_ADMIN_PASSWORD")
        if not password:
            if not sys.stdin.isatty():
                print(
                    "No users exist. Set AGENT_OBS_ADMIN_PASSWORD for non-interactive init.",
                    file=sys.stderr,
                )
                return 1
            password = getpass.getpass(f"Password for initial administrator {username!r}: ")
        try:
            user = await users.create_user(
                db,
                username=username,
                password=password,
                roles=["admin", "operator"],
                actor_type=audit.ACTOR_BOOTSTRAP,
            )
        except ValueError as exc:
            print(f"could not create administrator: {exc}", file=sys.stderr)
            return 1
        await db.commit()
        print(f"created initial administrator {user.username!r}")
        return 0


def _wait_for_command(
    command_id: int,
    home: str,
    *,
    expect_supervisor_exit: bool = False,
    timeout: float = 15.0,
) -> int:
    from agent_observatory.supervisor import read_supervisor_pid, system_command_status

    deadline = time.time() + timeout
    while time.time() < deadline:
        status = _run_async(system_command_status(command_id))
        if status == "failed":
            print(f"system command {command_id} failed", file=sys.stderr)
            return 1
        if status == "applied" and (
            not expect_supervisor_exit or read_supervisor_pid(home) is None
        ):
            print(f"system command {command_id} applied")
            return 0
        time.sleep(0.1)
    print(f"system command {command_id} timed out", file=sys.stderr)
    return 1


def _run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    async def run_and_close() -> T:
        from agent_observatory.db.session import reset_engine

        try:
            return await coro
        finally:
            await reset_engine()

    return asyncio.run(run_and_close())


async def _run_indexer(*, once: bool = False) -> int:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from agent_observatory.db.session import get_engine
    from agent_observatory.workers.indexer import IndexerWorker

    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    worker = IndexerWorker(factory)
    if once:
        indexed = await worker.scan_once()
        print(f"agent-observatory indexer: indexed {indexed} changed file(s)")
        return 0
    try:
        await worker.run()
    finally:
        await worker.stop()
    return 0


async def _run_user_passwd(args: list[str]) -> int:
    """Reset a user's password without knowing the current one.

    Local-console escape hatch for a forgotten administrator password: looks
    the user up, hashes the new password, revokes every active session and API
    token, and writes an audit row. No strength policy is applied (non-empty
    and matching prompts only); secrets never reach stdout or shell history.
    """
    import argparse
    import getpass

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from agent_observatory.db.session import get_engine
    from agent_observatory.services import audit, auth, users

    parser = argparse.ArgumentParser(
        prog="agent-observatory user-passwd",
        description="Reset a user's password (current password not required).",
    )
    parser.add_argument("username", nargs="?", default="admin", help="Defaults to admin.")
    parser.add_argument(
        "--password",
        help="New password. Omit to be prompted twice (keeps it out of history).",
    )
    ns = parser.parse_args(args)

    password = ns.password
    if not password:
        if not sys.stdin.isatty():
            print(
                "agent-observatory: no TTY for a password prompt; pass --password",
                file=sys.stderr,
            )
            return 1
        first = getpass.getpass(f"New password for {ns.username!r}: ")
        second = getpass.getpass("Repeat new password: ")
        if first != second:
            print("agent-observatory: passwords do not match", file=sys.stderr)
            return 1
        if not first:
            print("agent-observatory: password must not be empty", file=sys.stderr)
            return 1
        password = first

    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with factory() as db:
        user = await users.by_username(db, ns.username)
        if user is None:
            print(f"agent-observatory: user {ns.username!r} not found", file=sys.stderr)
            return 1
        user.password_hash = auth.hash_password(password)
        revoked_sessions, revoked_tokens = await users.revoke_all_credentials(db, user.id)
        await audit.record(
            db,
            action="user.password_reset",
            actor_type=audit.ACTOR_BOOTSTRAP,
            resource_type="user",
            resource_id=user.id,
            detail={"username": user.username, "credentials_revoked": True},
        )
        await db.commit()
        username = user.username
    print(
        f"password updated for {username!r}; revoked {revoked_sessions} session(s) "
        f"and {revoked_tokens} API token(s)"
    )
    return 0


async def _run_worker() -> int:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from agent_observatory.db.session import get_engine
    from agent_observatory.workers.indexer import IndexerWorker

    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    indexer = IndexerWorker(factory)
    try:
        await indexer.run()
    finally:
        await indexer.stop()
    return 0


async def _run_user_add(args: list[str]) -> int:
    """Provision a user with a hashed password and one or more roles.

    Password source precedence: `--password`, then `AGENT_OBS_USER_PASSWORD`,
    then an interactive prompt (keeps secrets out of shell history). Reuses
    `services.users.create_user`, which seeds roles and writes an audit row.
    """
    import argparse
    import getpass
    import os

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from agent_observatory.db.session import get_engine
    from agent_observatory.services import audit, users

    parser = argparse.ArgumentParser(
        prog="agent-observatory user-add",
        description="Provision a user (idempotent role seed + hashed password).",
    )
    parser.add_argument("username")
    parser.add_argument(
        "--role",
        dest="roles",
        action="append",
        choices=sorted(users.KNOWN_ROLES),
        help="Grant a role (repeatable). Defaults to viewer.",
    )
    parser.add_argument(
        "--password",
        help="Password (>=10 chars). Omit to read AGENT_OBS_USER_PASSWORD or prompt.",
    )
    ns = parser.parse_args(args)

    roles = ns.roles or ["viewer"]
    password = ns.password or os.environ.get("AGENT_OBS_USER_PASSWORD")
    if not password:
        password = getpass.getpass("Password: ")

    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with factory() as db:
        try:
            user = await users.create_user(
                db,
                username=ns.username,
                password=password,
                roles=roles,
                actor_type=audit.ACTOR_BOOTSTRAP,
            )
        except ValueError as exc:
            await db.rollback()
            print(f"agent-observatory: {exc}", file=sys.stderr)
            return 2
        await db.commit()
        role_names = sorted(user.role_names)
        user_id = user.id
    print(f"created user {ns.username!r} with roles {role_names} (id={user_id})")
    return 0
