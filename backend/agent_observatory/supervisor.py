"""Local process supervisor used by the development CLI.

The supervisor deliberately keeps runtime state outside PostgreSQL.  Process
control *requests* are persisted in ``system_commands`` by the CLI when the
database is reachable; this module is the executor and the local status/log
owner.  Runtime files are disposable and live below ``AGENT_OBS_HOME``.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _proc_start_token(pid: int) -> str | None:
    """Return Linux process start ticks, used to avoid PID reuse hazards."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split()
        return fields[21] if len(fields) > 21 else None
    except (FileNotFoundError, PermissionError, OSError, ValueError):
        return None


def pid_matches(pid: int, start_token: str | None) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split()
        if len(fields) > 2 and fields[2] == "Z":
            return False
    except (FileNotFoundError, PermissionError, OSError):
        return False
    if start_token is None:
        return True
    current = _proc_start_token(pid)
    return current is not None and current == start_token


@dataclass(slots=True)
class ProcessSpec:
    name: str
    command: list[str]
    cwd: str | None = None
    env: dict[str, str] | None = None


@dataclass(slots=True)
class _Runtime:
    process: subprocess.Popen[str] | None = None
    thread: threading.Thread | None = None
    failures: list[float] = field(default_factory=list)


class Supervisor:
    """Supervise a small, explicit set of local child processes.

    The class is intentionally synchronous so it can be used from both the
    CLI and focused unit tests.  ``run_forever`` provides the daemon loop;
    ``monitor_once`` is the deterministic unit-test seam.
    """

    BACKOFF = (1, 2, 4, 8, 16, 30)
    CRASH_WINDOW = 120.0
    CRASH_LIMIT = 5
    LOG_MAX_BYTES = 10 * 1024 * 1024
    LOG_BACKUPS = 5

    def __init__(
        self,
        home: str | Path,
        specs: dict[str, ProcessSpec] | None = None,
        *,
        poll_interval: float = 0.5,
    ) -> None:
        self.home = Path(home).expanduser()
        self.runtime_dir = self.home / "run"
        self.logs_dir = self.home / "logs"
        self.state_path = self.runtime_dir / "supervisor-state.json"
        self.pid_path = self.runtime_dir / "supervisor.pid"
        self.lock_path = self.runtime_dir / "supervisor.lock"
        self.state_lock_path = self.runtime_dir / "supervisor-state.lock"
        self.wakeup_path = self.runtime_dir / "supervisor.wakeup"
        self.poll_interval = poll_interval
        self.specs = specs or default_specs()
        self._runtime = {name: _Runtime() for name in self.specs}
        self._stop_event = threading.Event()
        self._last_command_poll = 0.0
        self.claim_owner = f"supervisor-{socket.gethostname()}-{os.getpid()}"
        self.instance_id = supervisor_instance_id(self.home)
        self._async_loop: asyncio.AbstractEventLoop | None = None
        self._run_forever_active = False
        self._lock_handle: Any = None
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    @property
    def running(self) -> bool:
        return pid_matches(os.getpid(), _proc_start_token(os.getpid()))

    def _read_state(self) -> dict[str, Any]:
        self.state_lock_path.touch(exist_ok=True)
        with self.state_lock_path.open("r") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
            try:
                value = json.loads(self.state_path.read_text())
                return (
                    cast(dict[str, Any], value)
                    if isinstance(value, dict)
                    else {"components": {}}
                )
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                return {"components": {}}
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _write_state(self, state: dict[str, Any]) -> None:
        self.state_lock_path.touch(exist_ok=True)
        with self.state_lock_path.open("r+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            state["updated_at"] = _now()
            tmp = self.runtime_dir / (
                f".supervisor-state.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            try:
                tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
                os.replace(tmp, self.state_path)
            finally:
                tmp.unlink(missing_ok=True)
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _component_state(self, name: str) -> dict[str, Any]:
        state = self._read_state()
        components = state.setdefault("components", {})
        if not isinstance(components, dict):
            components = {}
            state["components"] = components
        component = components.setdefault(name, {})
        return cast(dict[str, Any], component)

    def _write_pid_file(self) -> None:
        self.pid_path.write_text(f"{os.getpid()} {_proc_start_token(os.getpid()) or ''}\n")

    def _acquire_lock(self) -> None:
        handle = self.lock_path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise RuntimeError("another supervisor instance is already running") from None
        self._lock_handle = handle

    def _release_lock(self) -> None:
        if self._lock_handle is None:
            return
        with suppress(OSError):
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
        self._lock_handle.close()
        self._lock_handle = None

    def _clear_pid_file(self) -> None:
        try:
            data = self.pid_path.read_text().split()
            if data and int(data[0]) == os.getpid():
                self.pid_path.unlink(missing_ok=True)
        except (FileNotFoundError, OSError, ValueError):
            pass

    def _append_log(self, name: str, message: str, stream: str = "stdout") -> None:
        from agent_observatory.services.redaction import redact_secrets

        message = redact_secrets(message) or ""
        record = {"ts": _now(), "component": name, "stream": stream, "message": message}
        path = self.logs_dir / f"{name}.jsonl"
        if path.exists() and path.stat().st_size >= self.LOG_MAX_BYTES:
            oldest = path.with_suffix(path.suffix + f".{self.LOG_BACKUPS}")
            oldest.unlink(missing_ok=True)
            for index in range(self.LOG_BACKUPS - 1, 0, -1):
                source = path.with_suffix(path.suffix + f".{index}")
                if source.exists():
                    source.replace(path.with_suffix(path.suffix + f".{index + 1}"))
            path.replace(path.with_suffix(path.suffix + ".1"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _pump_output(self, name: str, pipe: Any) -> None:
        try:
            for line in iter(pipe.readline, ""):
                self._append_log(name, line.rstrip("\n"))
        finally:
            pipe.close()

    def _spawn(self, name: str) -> bool:
        spec = self.specs[name]
        runtime = self._runtime[name]
        if runtime.process and runtime.process.poll() is None:
            return True
        env = os.environ.copy()
        if spec.env:
            env.update(spec.env)
        try:
            process = subprocess.Popen(
                spec.command,
                cwd=spec.cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except OSError as exc:
            self._append_log(name, f"spawn failed: {exc}", "stderr")
            self._mark_exit(name, None, str(exc))
            return False
        runtime.process = process
        runtime.thread = threading.Thread(
            target=self._pump_output,
            args=(name, process.stdout),
            name=f"agent-observatory-log-{name}",
            daemon=True,
        )
        runtime.thread.start()
        state = self._read_state()
        component = state.setdefault("components", {}).setdefault(name, {})
        component.update(
            {
                "pid": process.pid,
                "pid_start_token": _proc_start_token(process.pid),
                "status": "running",
                "started_at": _now(),
                "command": spec.command,
                "restart_count": int(component.get("restart_count", 0)),
                "next_restart_at": None,
                "last_exit_code": None,
                "last_error": None,
            }
        )
        self._write_state(state)
        return True

    def _mark_exit(self, name: str, code: int | None, error: str | None = None) -> None:
        runtime = self._runtime[name]
        runtime.process = None
        now = time.time()
        runtime.failures = [stamp for stamp in runtime.failures if now - stamp <= self.CRASH_WINDOW]
        runtime.failures.append(now)
        state = self._read_state()
        component = state.setdefault("components", {}).setdefault(name, {})
        count = int(component.get("restart_count", 0)) + 1
        delay = (
            60
            if len(runtime.failures) >= self.CRASH_LIMIT
            else self.BACKOFF[min(len(runtime.failures) - 1, len(self.BACKOFF) - 1)]
        )
        component.update(
            {
                "pid": None,
                "pid_start_token": None,
                "status": "crash-loop" if len(runtime.failures) >= self.CRASH_LIMIT else "backoff",
                "restart_count": count,
                "last_exit_code": code,
                "last_error": error,
                "next_restart_at": time.time() + delay,
                "last_exit_at": _now(),
            }
        )
        self._write_state(state)

    def start(self, component: str | None = None) -> dict[str, Any]:
        names = [component] if component else list(self.specs)
        failed: list[str] = []
        for name in names:
            if name not in self.specs:
                raise ValueError(f"unknown component: {name}")
            self._reconcile_component(name)
            if not self._spawn(name):
                failed.append(name)
        if failed:
            raise RuntimeError(f"failed to start components: {', '.join(failed)}")
        self._write_pid_file()
        return self.status()

    def _reconcile_component(self, name: str) -> None:
        """Remove an orphan from a previous Supervisor before spawning."""
        info = self._read_state().get("components", {}).get(name, {})
        pid = info.get("pid")
        token = info.get("pid_start_token")
        runtime = self._runtime[name]
        if (
            isinstance(pid, int)
            and pid_matches(pid, token)
            and (runtime.process is None or runtime.process.pid != pid)
        ):
            self._terminate_persisted(pid, token, timeout=10.0)

    @staticmethod
    def _terminate_persisted(
        pid: int, start_token: str | None, *, timeout: float
    ) -> None:
        with suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pid, signal.SIGTERM)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not pid_matches(pid, start_token):
                return
            time.sleep(0.05)
        if pid_matches(pid, start_token):
            with suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pid, signal.SIGKILL)

    def stop(self, component: str | None = None, *, timeout: float = 10.0) -> dict[str, Any]:
        names = [component] if component else list(self.specs)
        state = self._read_state()
        for name in names:
            if name not in self.specs:
                raise ValueError(f"unknown component: {name}")
            runtime = self._runtime[name]
            process = runtime.process
            if process and process.poll() is None:
                self._terminate_process(process, timeout)
            else:
                info = state.get("components", {}).get(name, {})
                pid = info.get("pid")
                token = info.get("pid_start_token")
                if isinstance(pid, int) and pid_matches(pid, token):
                    self._terminate_persisted(pid, token, timeout=timeout)
            state.setdefault("components", {}).setdefault(name, {}).update(
                {"pid": None, "pid_start_token": None, "status": "stopped", "next_restart_at": None}
            )
        if component is None:
            self._clear_pid_file()
        self._write_state(state)
        if (
            component is None
            and not self._run_forever_active
            and self._async_loop is not None
        ):
            self._async_loop.close()
            self._async_loop = None
        return self.status()

    def _terminate_process(self, process: subprocess.Popen[str], timeout: float) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=timeout)
        except (ProcessLookupError, OSError):
            return
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError, OSError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)

    def restart(self, component: str | None = None) -> dict[str, Any]:
        self.stop(component)
        return self.start(component)

    def monitor_once(self) -> dict[str, Any]:
        if time.time() - self._last_command_poll >= 2:
            self._last_command_poll = time.time()
            self.process_commands_once()
        for name in self.specs:
            runtime = self._runtime[name]
            process = runtime.process
            if process is not None:
                code = process.poll()
                if code is not None:
                    self._mark_exit(name, code)
                    process = None
            if process is None:
                info = self._component_state(name)
                if info.get("status") == "stopped":
                    continue
                next_restart = info.get("next_restart_at")
                if next_restart and float(next_restart) > time.time():
                    continue
                self._spawn(name)
        return self.status()

    def process_commands_once(self) -> None:
        """Claim and apply one pending durable system command.

        Database failures are deliberately non-fatal: local child supervision
        continues and the next poll retries.  The command row remains pending
        until PostgreSQL is available.
        """
        try:
            if self._async_loop is None:
                self._async_loop = asyncio.new_event_loop()
            self._async_loop.run_until_complete(self._process_commands_once())
        except Exception as exc:  # noqa: BLE001 - DB outage must not kill supervisor
            self._append_log("supervisor", f"system command poll failed: {exc}", "stderr")

    async def _process_commands_once(self) -> None:
        from datetime import datetime

        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from agent_observatory.db.models import SystemCommand
        from agent_observatory.db.session import get_engine
        from agent_observatory.services import audit

        factory = async_sessionmaker(get_engine(), expire_on_commit=False)
        async with factory() as db:
            stale_before = datetime.now(UTC) - timedelta(seconds=60)
            stale_rows = list(
                (
                    await db.scalars(
                        select(SystemCommand).where(
                            SystemCommand.target_instance == self.instance_id,
                            SystemCommand.status == "claimed",
                            SystemCommand.claimed_at < stale_before,
                        )
                    )
                ).all()
            )
            for stale in stale_rows:
                stale.status = "pending"
                stale.claim_owner = None
                stale.claimed_at = None
            row = await db.scalar(
                select(SystemCommand)
                .where(
                    SystemCommand.target_instance == self.instance_id,
                    SystemCommand.status == "pending",
                )
                .order_by(SystemCommand.created_at, SystemCommand.id)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return
            row.status = "claimed"
            row.claim_owner = self.claim_owner
            row.claimed_at = datetime.now(UTC)
            await db.commit()
            command_id = row.id
            command_type = row.command_type
            component = row.component

        error: str | None = None
        try:
            if command_type == "up":
                self.start(component)
            elif command_type == "down":
                self.stop(component)
                if component is None or component == "worker":
                    await self._mark_worker_heartbeats_stopped(factory)
                if component is None:
                    self.request_stop()
            elif command_type == "restart":
                self.restart(component)
            elif command_type == "retention.purge":
                await self._apply_retention_purge(factory, command_id)
            else:
                raise ValueError(f"unsupported system command: {command_type}")
        except Exception as exc:  # noqa: BLE001 - persist failed outcome
            error = str(exc)[:500]

        async with factory() as db:
            row = await db.get(SystemCommand, command_id)
            if row is None:
                return
            row.status = "failed" if error else "applied"
            row.applied_at = datetime.now(UTC)
            row.error_kind = "supervisor_apply" if error else None
            row.error_message = error
            await audit.record(
                db,
                action=f"system.{command_type}.applied",
                actor_type=audit.ACTOR_SYSTEM,
                resource_type="system_command",
                resource_id=str(command_id),
                detail={"component": component, "status": row.status},
                result=audit.RESULT_ERROR if error else audit.RESULT_SUCCESS,
                error_kind="supervisor_apply" if error else None,
                error_message=error,
            )
            await db.commit()

    async def _apply_retention_purge(self, factory: Any, command_id: int) -> None:
        """Delete old local data for a previewed retention run.

        The guards live in ``services.retention`` so they stay unit-testable
        without a supervisor; this method writes the supervisor log trail.
        """
        from agent_observatory.db.models import SystemCommand
        from agent_observatory.services import retention

        async with factory() as db:
            row = await db.get(SystemCommand, command_id)
            payload: dict[str, Any] = dict(row.payload or {}) if row is not None else {}
        run_id = payload.get("run_id")
        self._append_log("supervisor", f"applying retention purge run={run_id}")
        result = await retention.apply_command(factory, payload)
        summary = ", ".join(
            f"{key}={value}"
            for key, value in sorted(result.items())
            if isinstance(value, (int, str))
        )
        self._append_log(
            "supervisor", f"retention purge run={run_id} finished: {summary}"
        )

    @staticmethod
    async def _mark_worker_heartbeats_stopped(factory: Any) -> None:
        from sqlalchemy import update

        from agent_observatory.db.models import WorkerHeartbeat

        async with factory() as db:
            await db.execute(
                update(WorkerHeartbeat)
                .where(
                    WorkerHeartbeat.worker_type.in_(
                        ("indexer",)
                    )
                )
                .values(status="stopped", pid=0, last_error=None)
            )
            await db.commit()

    def run_forever(self) -> None:
        self._acquire_lock()
        self._run_forever_active = True
        self._write_pid_file()
        state = self._read_state()
        components = state.setdefault("components", {})
        for name in self.specs:
            components.setdefault(name, {"status": "stopped"})
        self._write_state(state)
        # Startup is also a durable command.  The CLI persists `up` before it
        # launches this daemon, so claim it before entering the monitor loop.
        self.process_commands_once()
        try:
            while not self._stop_event.wait(self.poll_interval):
                self.monitor_once()
        finally:
            self.stop()
            self._run_forever_active = False
            if self._async_loop is not None:
                self._async_loop.close()
                self._async_loop = None
            self._release_lock()

    def request_stop(self) -> None:
        self._stop_event.set()

    def status(self) -> dict[str, Any]:
        state = self._read_state()
        components = state.setdefault("components", {})
        for name in self.specs:
            info = components.setdefault(name, {})
            pid = info.get("pid")
            if isinstance(pid, int) and info.get("status") == "running" and not pid_matches(pid, info.get("pid_start_token")):
                info.update({"status": "stale", "pid": None, "pid_start_token": None})
        supervisor_pid: int | None = None
        supervisor_token: str | None = None
        try:
            fields = self.pid_path.read_text().split()
            if fields:
                candidate = int(fields[0])
                token = fields[1] if len(fields) > 1 and fields[1] else None
                if pid_matches(candidate, token):
                    supervisor_pid, supervisor_token = candidate, token
        except (FileNotFoundError, OSError, ValueError):
            pass
        state["supervisor_pid"] = supervisor_pid
        state["supervisor_pid_start_token"] = supervisor_token
        return state

    def tail_logs(self, component: str = "all", lines: int = 100) -> list[str]:
        names = sorted(
            {path.name.split(".jsonl", 1)[0] for path in self.logs_dir.glob("*.jsonl*")}
        )
        if component != "all":
            names = [component]
        paths: list[Path] = []
        for name in names:
            current = self.logs_dir / f"{name}.jsonl"
            paths.extend(
                path
                for path in (
                    *(
                        current.with_suffix(current.suffix + f".{index}")
                        for index in range(self.LOG_BACKUPS, 0, -1)
                    ),
                    current,
                )
                if path.is_file()
            )
        records: list[tuple[str, str]] = []
        for path in paths:
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        timestamp = str(json.loads(line).get("ts") or "")
                    except (json.JSONDecodeError, AttributeError):
                        timestamp = ""
                    records.append((timestamp, line))
            except FileNotFoundError:
                continue
        records.sort(key=lambda item: item[0])
        return [line for _, line in records[-max(0, lines) :]]

    def follow_logs(self, component: str = "all", interval: float = 0.5) -> Iterator[str]:
        seen = 0
        while True:
            records = self.tail_logs(component, lines=10_000)
            yield from records[seen:]
            seen = len(records)
            time.sleep(interval)

    def doctor(self) -> dict[str, Any]:
        from agent_observatory.config import settings
        from agent_observatory.db.session import ping_database, reset_engine

        checks: dict[str, Any] = {}
        for executable in ("python", "node", "npm"):
            checks[executable] = {"ok": shutil.which(executable) is not None}
        checks["runtime_dir"] = {"ok": self.runtime_dir.exists(), "path": str(self.runtime_dir)}
        checks["logs_dir"] = {"ok": self.logs_dir.exists(), "path": str(self.logs_dir)}
        api_live = _api_live(settings().host, settings().port)
        checks["api_port"] = {
            "ok": api_live or _port_available(settings().host, settings().port),
            "port": settings().port,
            "state": "observatory" if api_live else "available",
        }
        root = Path(__file__).resolve().parents[2]
        checks["frontend_build"] = {
            "ok": (root / "frontend" / "dist" / "index.html").is_file(),
            "path": str(root / "frontend" / "dist" / "index.html"),
        }

        async def dependency_checks() -> tuple[bool, bool, bool]:
            from sqlalchemy import func, select
            from sqlalchemy.ext.asyncio import async_sessionmaker

            from agent_observatory.db.models import User, WorkerHeartbeat
            from agent_observatory.db.session import get_engine

            database_ok = False
            admin_ok = heartbeat_ok = False
            try:
                database_ok = await ping_database()
                factory = async_sessionmaker(get_engine(), expire_on_commit=False)
                async with factory() as db:
                    admin_ok = bool(
                        await db.scalar(
                            select(func.count())
                            .select_from(User)
                            .where(User.is_active.is_(True))
                        )
                    )
                    fresh_after = datetime.now(UTC) - timedelta(
                        seconds=max(settings().worker_heartbeat_interval * 3, 30)
                    )
                    heartbeat_ok = bool(
                        await db.scalar(
                            select(func.count())
                            .select_from(WorkerHeartbeat)
                            .where(WorkerHeartbeat.heartbeat_at >= fresh_after)
                        )
                    )
            except Exception:  # noqa: BLE001
                database_ok = False
            finally:
                await reset_engine()
            return database_ok, admin_ok, heartbeat_ok

        database_ok, admin_ok, heartbeat_ok = asyncio.run(dependency_checks())
        checks["postgresql"] = {"ok": database_ok}
        checks["administrator"] = {"ok": admin_ok}
        supervisor_running = read_supervisor_pid(self.home) is not None
        checks["worker_heartbeat"] = {
            "ok": heartbeat_ok or not supervisor_running,
            "state": (
                "fresh"
                if heartbeat_ok
                else "not-required-until-up"
                if not supervisor_running
                else "stale-or-missing"
            ),
        }
        env_path = Path(__file__).resolve().parent.parent / ".env"
        mode = env_path.stat().st_mode & 0o777 if env_path.exists() else None
        checks["config_permissions"] = {
            "ok": mode is None or mode & 0o077 == 0,
            "mode": oct(mode) if mode is not None else None,
        }
        checks["supervisor_state"] = {
            "ok": not any(
                component.get("status") == "stale"
                for component in self.status().get("components", {}).values()
            )
        }
        backend = Path(__file__).resolve().parent.parent
        migration = subprocess.run(
            [sys.executable, "-m", "alembic", "check"],
            cwd=backend,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        checks["migrations"] = {"ok": migration.returncode == 0}
        return {"ok": all(item.get("ok", False) for item in checks.values()), "checks": checks}


def _port_available(host: str, port: int) -> bool:
    import socket

    with socket.socket() as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) != 0


def _api_live(host: str, port: int) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}/api/health/live", timeout=0.5
        ) as response:
            return bool(response.status == 200)
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def default_specs() -> dict[str, ProcessSpec]:
    from agent_observatory.config import settings

    backend = Path(__file__).resolve().parent.parent
    python = sys.executable
    config = settings()
    return {
        "api": ProcessSpec(
            "api",
            [
                python,
                "-m",
                "uvicorn",
                "agent_observatory.main:app",
                "--host",
                config.host,
                "--port",
                str(config.port),
            ],
            cwd=str(backend),
        ),
        "worker": ProcessSpec(
            "worker",
            [python, "-c", "import asyncio; from agent_observatory.cli import _run_worker; asyncio.run(_run_worker())"],
            cwd=str(backend),
        ),
    }


def dev_specs() -> dict[str, ProcessSpec]:
    specs = default_specs()
    root = Path(__file__).resolve().parents[2]
    npm = shutil.which("npm") or "/opt/node22/bin/npm"
    specs["frontend"] = ProcessSpec("frontend", [npm, "run", "dev"], cwd=str(root / "frontend"))
    return specs


def read_supervisor_pid(home: str | Path) -> tuple[int, str | None] | None:
    path = Path(home).expanduser() / "run" / "supervisor.pid"
    try:
        fields = path.read_text().split()
        if not fields:
            return None
        pid = int(fields[0])
        token = fields[1] if len(fields) > 1 and fields[1] else None
        return (pid, token) if pid_matches(pid, token) else None
    except (FileNotFoundError, OSError, ValueError):
        return None


def signal_supervisor(home: str | Path, sig: int = signal.SIGTERM) -> bool:
    identity = read_supervisor_pid(home)
    if not identity:
        return False
    pid, _ = identity
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def supervisor_instance_id(home: str | Path) -> str:
    identity = f"{socket.gethostname()}\0{Path(home).expanduser().resolve()}"
    return hashlib.sha256(identity.encode()).hexdigest()


async def enqueue_system_command(
    command_type: str, component: str | None = None, *, home: str | Path
) -> int:
    """Persist a control request and audit it; callers wake the supervisor."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from agent_observatory.db.models import SystemCommand
    from agent_observatory.db.session import get_engine
    from agent_observatory.services import audit

    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with factory() as db:
        row = SystemCommand(
            command_type=command_type,
            component=component,
            target_instance=supervisor_instance_id(home),
            requested_by="cli",
            payload={},
        )
        db.add(row)
        await db.flush()
        await audit.record(db, action=f"system.{command_type}.requested", actor_type=audit.ACTOR_SYSTEM, resource_type="system_command", resource_id=str(row.id), detail={"component": component})
        await db.commit()
        return int(row.id)


async def system_command_status(command_id: int) -> str | None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from agent_observatory.db.models import SystemCommand
    from agent_observatory.db.session import get_engine

    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with factory() as db:
        row = await db.get(SystemCommand, command_id)
        return row.status if row is not None else None


def _module_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true")
    ns = parser.parse_args(argv)
    if not ns.run:
        parser.error("only --run is supported")
    from agent_observatory.config import settings

    supervisor = Supervisor(settings().home)

    def _handle_signal(signum: int, _frame: Any) -> None:
        supervisor._append_log("supervisor", f"received signal {signum}")
        supervisor.request_stop()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    supervisor.run_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_module_main())


__all__ = [
    "ProcessSpec",
    "Supervisor",
    "default_specs",
    "dev_specs",
    "enqueue_system_command",
    "pid_matches",
    "read_supervisor_pid",
    "signal_supervisor",
    "system_command_status",
]
