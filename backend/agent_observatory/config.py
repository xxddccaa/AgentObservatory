"""Application settings from environment.

Reads environment with explicit, documented aliases (see `.env.example`).
One source of truth consumed by `db.session`, `services.auth`, workers, and
the CLI. Pydantic Settings validates types and fails fast on typos.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_list(value):  # accepts str or iterable, returns list[str]
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v) for v in value]


def _unique_paths(paths: list[str]) -> list[str]:
    """Expand ``~`` and drop paths that resolve to the same real directory."""
    unique: dict[str, str] = {}
    for raw in paths:
        expanded = os.path.expanduser(str(raw))
        key = os.path.realpath(expanded)
        unique.setdefault(key, expanded)
    return list(unique.values())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Identity ---
    app_name: str = "agent-observatory"
    version: str = "0.1.0"
    environment: str = Field(default="development", alias="AGENT_OBS_ENVIRONMENT")

    # --- Hosting ---
    host: str = Field(default="127.0.0.1", alias="AGENT_OBS_HOST")
    port: int = Field(default=8125, alias="AGENT_OBS_PORT")
    log_level: str = Field(default="INFO", alias="AGENT_OBS_LOG_LEVEL")

    # --- Data stores ---
    database_url: str = Field(
        default=(
            "postgresql+asyncpg://observatory:change-me@127.0.0.1:55433/agent_observatory"
        ),
        alias="AGENT_OBS_DATABASE_URL",
    )
    # --- Paths ---
    home: str = Field(
        default=os.path.expanduser("~/.agent-observatory"),
        alias="AGENT_OBS_HOME",
    )
    codex_home: str = Field(
        default=os.path.expanduser("~/.codex"),
        alias="AGENT_OBS_CODEX_HOME",
    )
    pi_home: str = Field(
        default="~/.pi",
        alias="AGENT_OBS_PI_HOME",
    )
    xdcode_home: str = Field(
        default="~/.xdcode",
        alias="AGENT_OBS_XDCODE_HOME",
    )
    stepgo_home: str = Field(
        default="~/.stepgo",
        alias="AGENT_OBS_STEPGO_HOME",
    )

    # --- Indexer runtime ---
    worker_heartbeat_interval: float = Field(
        default=10.0, alias="AGENT_OBS_WORKER_HEARTBEAT_INTERVAL", gt=0
    )
    indexer_poll_interval: float = Field(
        default=2.0, alias="AGENT_OBS_INDEXER_POLL_INTERVAL", gt=0
    )
    discovery_enabled: bool = Field(
        default=True, alias="AGENT_OBS_DISCOVERY_ENABLED"
    )
    insights_enabled: bool = Field(
        default=True, alias="AGENT_OBS_INSIGHTS_ENABLED"
    )
    memories_enabled: bool = Field(
        default=True, alias="AGENT_OBS_MEMORIES_ENABLED"
    )
    insights_backfill_days: int = Field(
        default=90, alias="AGENT_OBS_INSIGHTS_BACKFILL_DAYS", ge=1, le=3650
    )
    local_source_poll_interval: float = Field(
        default=5.0, alias="AGENT_OBS_LOCAL_SOURCE_POLL_INTERVAL", gt=0
    )
    # Extra, non-standard discovery roots as CSV of "agent:path" items, e.g.
    # "codex:/data/.codex/sessions,dsh:/data/.dsh/sessions". Augments AGENT_LAYOUTS.
    extra_roots_csv: str = Field(default="", alias="AGENT_OBS_EXTRA_ROOTS")

    # --- Retention (local data cleanup) ---
    # Master switch for the Storage page. When false the overview and preview
    # stay available (read-only) and every run is refused.
    retention_enabled: bool = Field(
        default=True, alias="AGENT_OBS_RETENTION_ENABLED"
    )
    # Hard floor: nothing modified inside this window is ever deletable, no
    # matter what the caller asks for. 24h keeps in-flight sessions safe.
    retention_min_hours: int = Field(
        default=24, alias="AGENT_OBS_RETENTION_MIN_HOURS", ge=1, le=8760
    )
    # Roots that stay read-only even when selected (CSV, absolute paths).
    # Example: "~/.codex/sessions,~/.claude/projects".
    retention_protected_roots_csv: str = Field(
        default="", alias="AGENT_OBS_RETENTION_PROTECTED_ROOTS"
    )
    # Upper bound on filesystem entries visited while planning one run, so a
    # pathological tree cannot make a preview unbounded.
    retention_max_entries: int = Field(
        default=250_000, alias="AGENT_OBS_RETENTION_MAX_ENTRIES", ge=1000
    )
    # Planning above this size needs an explicit second acknowledgement.
    retention_large_run_bytes: int = Field(
        default=5 * 1024**3, alias="AGENT_OBS_RETENTION_LARGE_RUN_BYTES", ge=0
    )
    # Seconds the overview size scan is cached for.
    retention_overview_ttl: float = Field(
        default=60.0, alias="AGENT_OBS_RETENTION_OVERVIEW_TTL", gt=0
    )
    # Grace before a live plan counts as "changed" versus the preview.
    retention_plan_drift_bytes: int = Field(
        default=64 * 1024**2, alias="AGENT_OBS_RETENTION_PLAN_DRIFT_BYTES", ge=0
    )
    # Lifetime of the signed plan token handed to the browser.
    retention_plan_ttl: int = Field(
        default=1800, alias="AGENT_OBS_RETENTION_PLAN_TTL", ge=60, le=86_400
    )

    # --- Workspace policy ---
    # Stored as raw CSV so pydantic-settings' DotEnv source doesn't try to
    # JSON-decode a comma-separated list before any validator runs. Exposed as
    # a list via the `allowed_workspaces` property.
    allowed_workspaces_csv: str = Field(
        default="",
        alias="AGENT_OBS_ALLOWED_WORKSPACES",
    )
    # --- Security ---
    session_secret: str = Field(
        default="dev-only-change-me-use-random-64-chars",
        alias="AGENT_OBS_SESSION_SECRET",
    )
    api_token_secret: str = Field(
        default="dev-only-change-me-use-random-64-chars",
        alias="AGENT_OBS_API_TOKEN_SECRET",
    )
    bootstrap_token: str = Field(
        default="dev-only-bootstrap-admin-token",
        alias="AGENT_OBS_BOOTSTRAP_TOKEN",
    )

    # --- Test overrides (used by conftest only) ---
    test_database_url: str = Field(
        default="postgresql+asyncpg://observatory:change-me@127.0.0.1:55433/agent_observatory_ci",
        alias="TEST_DATABASE_URL",
    )
    # --- Computed ---
    @property
    def sessions_root(self) -> str:
        return os.path.join(os.path.expanduser(self.codex_home), "sessions")

    @property
    def stepcode_root(self) -> str:
        return os.getenv(
            "AGENT_OBS_STEPCODE_HOME", os.path.expanduser("~/.stepcode/codex")
        )

    @property
    def stepcli_home(self) -> str:
        return os.getenv(
            "AGENT_OBS_STEPCLI_HOME", os.path.expanduser("~/.step-cli")
        )

    @property
    def stepcli_sessions_root(self) -> str:
        return os.path.join(os.path.expanduser(self.stepcli_home), "sessions")

    @property
    def claude_projects_root(self) -> str:
        return os.getenv(
            "AGENT_OBS_CLAUDE_PROJECTS_ROOT",
            os.path.expanduser("~/.claude/projects"),
        )

    @property
    def xdcode_sessions_roots(self) -> list[str]:
        """Current and archived XDCode rollouts (archive is optional)."""
        home = os.path.expanduser(self.xdcode_home)
        return _unique_paths(
            [os.path.join(home, "sessions"), os.path.join(home, "archived_sessions")]
        )

    @property
    def pi_sessions_roots(self) -> list[str]:
        """Pi's native root plus the root used by StepGo-managed Pi runs."""
        pi_home = os.path.expanduser(self.pi_home)
        stepgo_home = os.path.expanduser(self.stepgo_home)
        return _unique_paths(
            [
                os.path.join(pi_home, "agent", "sessions"),
                os.path.join(stepgo_home, "agents", "pi", "sessions"),
            ]
        )

    @property
    def stepgo_host_root(self) -> str:
        return os.path.join(os.path.expanduser(self.stepgo_home), "sessions", "host")

    @property
    def discovery_roots(self) -> dict[str, list[str]]:
        """Effective roots shared by discovery, heartbeat and adapters."""
        roots = {
            "codex": [self.sessions_root],
            "stepcode": [
                self.stepcode_root,
                os.path.expanduser("~/.stepcode/claude/projects"),
            ],
            "stepcli": [self.stepcli_sessions_root],
            "claude": [self.claude_projects_root],
            "dsh": ["~/.dsh/sessions"],
            "opencode": ["~/.local/share/opencode", "~/.config/opencode"],
            "pi": self.pi_sessions_roots,
            "xdcode": self.xdcode_sessions_roots,
            # Only host run logs belong to StepGo itself.  Its managed agent
            # directories often symlink to native roots and must not be labelled
            # as StepGo sessions.
            "stepgo": [self.stepgo_host_root],
        }
        for agent, extra in self.extra_roots.items():
            roots.setdefault(agent, []).extend(extra)
        return {agent: _unique_paths(paths) for agent, paths in roots.items()}

    @property
    def local_source_paths(self) -> dict[str, str]:
        return {
            "thread_history": os.path.join(
                os.path.expanduser(self.codex_home), "thread_history_1.sqlite"
            ),
            "state": os.path.join(os.path.expanduser(self.codex_home), "state_5.sqlite"),
            "logs": os.path.join(os.path.expanduser(self.codex_home), "logs_2.sqlite"),
            "goals": os.path.join(os.path.expanduser(self.codex_home), "goals_1.sqlite"),
            "skills": os.path.join(os.path.expanduser(self.codex_home), "skills"),
        }

    @property
    def working_dirs(self):
        return {
            "runtime": self.home,
            "artifacts": os.path.join(self.home, "artifacts"),
            "exports": os.path.join(self.home, "exports"),
            "logs": os.path.join(self.home, "logs"),
            "run": os.path.join(self.home, "run"),
        }

    @property
    def allowed_workspaces(self) -> list[str]:
        return [v.strip() for v in self.allowed_workspaces_csv.split(",") if v.strip()]

    @property
    def extra_roots(self) -> dict[str, list[str]]:
        """Parse ``AGENT_OBS_EXTRA_ROOTS`` ("agent:path,...") into agent->paths."""
        result: dict[str, list[str]] = {}
        for item in self.extra_roots_csv.split(","):
            item = item.strip()
            if not item or ":" not in item:
                continue
            agent, path = item.split(":", 1)
            agent, path = agent.strip(), path.strip()
            if agent and path:
                result.setdefault(agent, []).append(path)
        return result

    @property
    def retention_protected_roots(self) -> list[str]:
        """Absolute protected roots; ``~`` is expanded and duplicates dropped."""
        seen: dict[str, None] = {}
        for raw in _split_list(self.retention_protected_roots_csv):
            seen.setdefault(os.path.abspath(os.path.expanduser(raw)), None)
        return list(seen)

    @model_validator(mode="after")
    def _forbid_prod_placeholders(self):
        secrets_ok = self.environment != "production"
        if not secrets_ok:
            for label, value in [
                ("session_secret", self.session_secret),
                ("api_token_secret", self.api_token_secret),
            ]:
                if value.startswith("dev-only-") or len(value) < 32:
                    raise ValueError(f"{label} must be a strong random value in production")
        return self


@lru_cache
def settings() -> Settings:
    # pydantic-settings already loads .env; called once.
    env_file = os.environ.get("AGENT_OBS_ENV_FILE", Settings.model_config["env_file"])
    s = Settings(_env_file=env_file)  # type: ignore[call-arg]
    return s


__all__ = ["Settings", "settings", "_split_list"]
