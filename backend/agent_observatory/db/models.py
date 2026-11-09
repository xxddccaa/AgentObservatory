"""SQLAlchemy models + metadata (Phase 1: auth / RBAC / audit foundation).

Phase 0 only exposed `metadata` so Alembic could reference it. Phase 1 adds a
`DeclarativeBase` subclass and the user/role/session/audit tables that every
later phase builds on. Mirrors the schema shape of resilient-codex-tasks-dev
(`users`, `roles`, `user_roles`, `sessions`, `api_tokens`, `audit_logs`)
but keeps only what Observatory needs.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

#: Shared metadata object. `alembic/env.py` and the async engine both read
#: `Base.metadata`, so registering models here automatically feeds migrations.
metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


class Base(DeclarativeBase):
    """Declarative base bound to the shared `metadata` object."""

    metadata = metadata


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )

    roles: Mapped[list[Role]] = relationship(
        "Role", secondary="user_roles", back_populates="users", lazy="selectin"
    )

    @property
    def role_names(self) -> set[str]:
        return {r.name for r in self.roles}


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)

    users: Mapped[list[User]] = relationship(
        "User", secondary="user_roles", back_populates="roles"
    )


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (Index("ix_user_roles_role_id", "role_id"),)

    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )


class Session(Base):
    """Web login session. The raw token lives only in the browser cookie; the
    DB stores `token_hash` = sha256(token)."""

    __tablename__ = "sessions"
    __table_args__ = (Index("ix_sessions_user_id", "user_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(Text)


class ApiToken(Base):
    """Bearer API token (programmatic access). Stored hashed like sessions."""

    __tablename__ = "api_tokens"
    __table_args__ = (Index("ix_api_tokens_user_id", "user_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    """Append-only record of every user and system action.

    `actor_type` ∈ {user, system, bootstrap}. `result` ∈ {success, failure,
    error}. The `detail` JSONB carries action-specific before/after/input
    summaries — never secrets (see AGENTS.md #19).
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_actor_created", "actor_id", "created_at"),
        Index("ix_audit_session_created", "session_id", "created_at"),
        Index("ix_audit_action_created", "action", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="SET NULL")
    )
    actor_type: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="user"
    )
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(255))
    request_id: Mapped[str | None] = mapped_column(String(64))
    session_id: Mapped[str | None] = mapped_column(String(64))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    result: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="success"
    )
    error_kind: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Workspace(Base, TimestampMixin):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_path: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    git_remote: Mapped[str | None] = mapped_column(Text)
    git_branch: Mapped[str | None] = mapped_column(String(255))
    is_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    workspace_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="unknown"
    )
    workspace_confidence: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="low"
    )


class SystemCommand(Base):
    """Durable local process-control command applied by the Supervisor."""

    __tablename__ = "system_commands"
    __table_args__ = (Index("ix_system_commands_pending", "status", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    command_type: Mapped[str] = mapped_column(String(64), nullable=False)
    component: Mapped[str | None] = mapped_column(String(32))
    target_instance: Mapped[str] = mapped_column(String(255), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="pending"
    )
    claim_owner: Mapped[str | None] = mapped_column(String(255))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_kind: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    worker_type: Mapped[str] = mapped_column(String(64), nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    pid: Mapped[int] = mapped_column(Integer, nullable=False)
    active_sessions: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="running"
    )
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_kind: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentSession(Base, TimestampMixin):
    __tablename__ = "agent_sessions"
    __table_args__ = (
        UniqueConstraint("rollout_real_path"),
        UniqueConstraint("source_identity"),
        Index("ix_agent_sessions_thread_id", "thread_id"),
        Index("ix_agent_sessions_workspace_updated", "workspace_id", "updated_at"),
        Index("ix_agent_sessions_source_kind", "source_kind"),
        Index("ix_agent_sessions_agent", "agent"),
        Index("ix_agent_sessions_agent_updated", "agent", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(255), nullable=False)
    rollout_path: Mapped[str | None] = mapped_column(Text)
    rollout_real_path: Mapped[str | None] = mapped_column(Text)
    rollout_format: Mapped[str] = mapped_column(String(16), nullable=False, server_default="jsonl")
    rollout_compression: Mapped[str | None] = mapped_column(String(16))
    agent: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="codex"
    )
    source_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="codex_jsonl"
    )
    source_identity: Mapped[str] = mapped_column(Text, nullable=False)
    source_path: Mapped[str | None] = mapped_column(Text)
    source_format: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="jsonl"
    )
    source_compression: Mapped[str | None] = mapped_column(String(16))
    cwd: Mapped[str | None] = mapped_column(Text)
    workspace_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("workspaces.id", ondelete="SET NULL"))
    git_branch: Mapped[str | None] = mapped_column(String(255))
    originator: Mapped[str | None] = mapped_column(String(255))
    cli_version: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(255))
    thread_name: Mapped[str | None] = mapped_column(String(255))
    ai_title: Mapped[str | None] = mapped_column(String(255))
    display_title: Mapped[str | None] = mapped_column(String(255))
    approval_mode: Mapped[str | None] = mapped_column(String(64))
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    total_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    cached_input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    cache_creation_input_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    reasoning_output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    is_ongoing: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    is_subagent: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    parent_session_id: Mapped[str | None] = mapped_column(String(64))
    history_base_thread_id: Mapped[str | None] = mapped_column(String(255))
    parser_version: Mapped[str] = mapped_column(String(32), nullable=False, server_default="1")
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    file_mtime_ns: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    file_sha256: Mapped[str | None] = mapped_column(String(64))
    last_indexed_offset: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    last_indexed_line: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    #: Highest ``agent_events.event_index`` already folded into messages /
    #: turns / tools.  ``-1`` means "not projected yet"; the scanner resets it
    #: to ``-1`` whenever it rebuilds a session's events.
    projected_event_index: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="-1"
    )
    index_status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="discovered")
    last_error: Mapped[str | None] = mapped_column(Text)


class IndexCheckpoint(Base, TimestampMixin):
    """Durable source cursor committed atomically with indexed events."""

    __tablename__ = "index_checkpoints"
    __table_args__ = (
        UniqueConstraint("source_identity"),
        UniqueConstraint("agent_session_id"),
        Index("ix_index_checkpoints_status_updated", "status", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_identity: Mapped[str] = mapped_column(Text, nullable=False)
    agent_session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("agent_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    cursor_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    fingerprint_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    parser_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="discovered"
    )
    last_error_kind: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StepGoRun(Base, TimestampMixin):
    """One StepGo host run, linked to an underlying native agent session.

    Host logs and native transcripts have different lifecycles, so runs keep
    their own cursor instead of occupying the one-checkpoint-per-agent-session
    table.  A run may be linked to one native session; a resumed session may be
    referenced by many runs.
    """

    __tablename__ = "stepgo_runs"
    __table_args__ = (
        UniqueConstraint("source_identity"),
        Index("ix_stepgo_runs_host_run_id", "host_run_id"),
        Index("ix_stepgo_runs_agent_session", "linked_agent_session_id", "start_time"),
        Index("ix_stepgo_runs_link_status", "link_status", "updated_at"),
        Index("ix_stepgo_runs_source_path", "source_path"),
    )

    parser_version: Mapped[str] = mapped_column(String(32), nullable=False, server_default="1")
    fingerprint_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    host_run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_identity: Mapped[str] = mapped_column(Text, nullable=False)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    agent: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="unknown"
    )
    module: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="unknown"
    )
    link_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="unlinked"
    )
    link_reason: Mapped[str | None] = mapped_column(String(64))
    linked_agent_session_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("agent_sessions.id", ondelete="SET NULL")
    )
    native_session_id: Mapped[str | None] = mapped_column(String(255))
    transcript_path: Mapped[str | None] = mapped_column(Text)
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    raw_start_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    raw_end_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    file_mtime_ns: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    file_sha256: Mapped[str | None] = mapped_column(String(64))
    last_indexed_offset: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    last_indexed_line: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    index_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="discovered"
    )
    last_error: Mapped[str | None] = mapped_column(Text)


class StepGoRunEvent(Base):
    """Raw StepGo host event.  Host output is evidence, never token history."""

    __tablename__ = "stepgo_run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "event_index"),
        Index("ix_stepgo_run_events_run_line", "run_id", "line_number"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("stepgo_runs.id", ondelete="CASCADE"), nullable=False
    )
    event_index: Mapped[int] = mapped_column(Integer, nullable=False)
    line_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    byte_offset: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    raw_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    is_known: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AgentEvent(Base):
    __tablename__ = "agent_events"
    __table_args__ = (
        UniqueConstraint("agent_session_id", "event_index"),
        Index("ix_agent_events_session_line", "agent_session_id", "line_number"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent_session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False
    )
    event_index: Mapped[int] = mapped_column(Integer, nullable=False)
    line_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    byte_offset: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    top_level_type: Mapped[str | None] = mapped_column(String(64))
    payload_type: Mapped[str | None] = mapped_column(String(128))
    normalized_type: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    raw_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    is_known: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    parser_version: Mapped[str] = mapped_column(String(32), nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AgentTurn(Base, TimestampMixin):
    __tablename__ = "agent_turns"
    __table_args__ = (
        UniqueConstraint("agent_session_id", "turn_index"),
        Index(
            "ix_agent_turns_activity_time",
            text("(coalesce(completed_at, started_at, created_at))"),
        ),
        Index("ix_agent_turns_started", "started_at"),
        Index("ix_agent_turns_outcome_started", "outcome", "started_at"),
        Index("ix_agent_turns_agent_started", "agent", "started_at"),
        Index("ix_agent_turns_agent_session", "agent", "agent_session_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="codex"
    )
    agent_session_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False)
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    turn_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="unknown")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    cached_input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    cache_creation_input_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    reasoning_output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    total_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    first_agent_latency_ms: Mapped[int | None] = mapped_column(BigInteger)
    active_tool_ms: Mapped[int | None] = mapped_column(BigInteger)
    validation_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="unknown"
    )
    outcome: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="unknown"
    )
    metric_confidence: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="derived"
    )
    error_kind: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)


class CodexMessage(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_session_sequence", "agent_session_id", "sequence"),
        Index("ix_messages_event_id", "event_id"),
        Index("ix_messages_turn_id", "turn_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_session_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False)
    event_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("agent_events.id", ondelete="SET NULL"))
    turn_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("agent_turns.id", ondelete="SET NULL"))
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    markdown_text: Mapped[str | None] = mapped_column(Text)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    is_system_generated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    copy_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ToolCall(Base):
    __tablename__ = "tool_calls"
    __table_args__ = (
        Index("ix_tool_calls_session_started", "agent_session_id", "started_at"),
        Index("ix_tool_calls_event_id", "event_id"),
        Index("ix_tool_calls_agent_started", "agent", "started_at"),
        Index("ix_tool_calls_agent_session", "agent", "agent_session_id"),
        Index("ix_tool_calls_turn_id", "turn_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="codex"
    )
    agent_session_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False)
    turn_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("agent_turns.id", ondelete="SET NULL"))
    event_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("agent_events.id", ondelete="SET NULL"))
    call_id: Mapped[str] = mapped_column(String(255), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    tool_kind: Mapped[str] = mapped_column(String(64), nullable=False, server_default="unknown")
    arguments_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    command_text: Mapped[str | None] = mapped_column(Text)
    output_text: Mapped[str | None] = mapped_column(Text)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="pending")
    is_mutating: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class LocalDataSource(Base, TimestampMixin):
    """One read-only local Codex data source and its durable ingestion cursor."""

    __tablename__ = "local_data_sources"
    __table_args__ = (
        UniqueConstraint("source_identity"),
        Index("ix_local_data_sources_kind_status", "source_kind", "status"),
        Index("ix_local_data_sources_agent_status", "agent", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="codex"
    )
    source_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    source_identity: Mapped[str] = mapped_column(Text, nullable=False)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    source_format: Mapped[str] = mapped_column(String(32), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(32), nullable=False)
    cursor_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    fingerprint_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="discovered"
    )
    record_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    byte_size: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    coverage_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    coverage_ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    backfill_cutoff_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_kind: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)


class EvidenceRecord(Base):
    """Raw audit evidence copied from non-rollout sources or linked to a rollout event."""

    __tablename__ = "evidence_records"
    __table_args__ = (
        UniqueConstraint("source_id", "source_record_id"),
        Index("ix_evidence_records_session_time", "agent_session_id", "recorded_at"),
        Index("ix_evidence_records_category_time", "category", "recorded_at"),
        Index("ix_evidence_records_thread_time", "thread_id", "recorded_at"),
        Index("ix_evidence_records_agent_event_id", "agent_event_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("local_data_sources.id", ondelete="CASCADE"), nullable=False
    )
    source_record_id: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_session_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("agent_sessions.id", ondelete="SET NULL")
    )
    agent_event_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agent_events.id", ondelete="SET NULL")
    )
    thread_id: Mapped[str | None] = mapped_column(String(255))
    turn_id: Mapped[str | None] = mapped_column(String(255))
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    level: Mapped[str | None] = mapped_column(String(32))
    target: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    raw_text: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    raw_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_locator: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ActivitySpan(Base):
    """Normalized timed activity used by aggregate charts and session waterfalls."""

    __tablename__ = "activity_spans"
    __table_args__ = (
        Index("ix_activity_spans_session_started", "agent_session_id", "started_at"),
        Index("ix_activity_spans_type_started", "span_type", "started_at"),
        Index("ix_activity_spans_agent_started", "agent", "started_at"),
        Index("ix_activity_spans_agent_session", "agent", "agent_session_id"),
        Index("ix_activity_spans_evidence_id", "evidence_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="codex"
    )
    source_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="rollout"
    )
    source_record_id: Mapped[str | None] = mapped_column(String(255))
    agent_session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False
    )
    evidence_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("evidence_records.id", ondelete="SET NULL")
    )
    thread_id: Mapped[str | None] = mapped_column(String(255))
    turn_id: Mapped[str | None] = mapped_column(String(255))
    span_type: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="unknown"
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    confidence: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="derived"
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SkillDefinition(Base, TimestampMixin):
    __tablename__ = "skill_definitions"
    __table_args__ = (
        UniqueConstraint("source_path"),
        Index("ix_skill_definitions_name", "name"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="user"
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class MemoryEntry(Base, TimestampMixin):
    """A read-only snapshot of one CLI-managed memory entry."""

    __tablename__ = "memory_entries"
    __table_args__ = (
        UniqueConstraint("source_id", "source_record_id"),
        Index("ix_memory_entries_agent_present", "agent", "is_present"),
        Index("ix_memory_entries_workspace_present", "workspace_id", "is_present"),
        Index("ix_memory_entries_source_updated", "source_updated_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("local_data_sources.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("workspaces.id", ondelete="SET NULL")
    )
    agent: Mapped[str] = mapped_column(String(32), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    source_record_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    thread_id: Mapped[str | None] = mapped_column(String(255))
    scope: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="project"
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_present: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    sensitivity: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="normal"
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class SessionSkillEvidence(Base):
    __tablename__ = "session_skill_evidence"
    __table_args__ = (
        UniqueConstraint(
            "agent_session_id",
            "skill_name",
            "evidence_type",
            "source_record_id",
        ),
        Index("ix_session_skill_evidence_skill_time", "skill_name", "event_time"),
        Index("ix_session_skill_evidence_evidence_id", "evidence_id"),
        Index("ix_session_skill_evidence_skill_id", "skill_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False
    )
    skill_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("skill_definitions.id", ondelete="SET NULL")
    )
    evidence_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("evidence_records.id", ondelete="SET NULL")
    )
    turn_id: Mapped[str | None] = mapped_column(String(255))
    skill_name: Mapped[str] = mapped_column(String(255), nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[str] = mapped_column(String(16), nullable=False)
    source_record_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ThreadEdge(Base):
    __tablename__ = "thread_edges"
    __table_args__ = (
        UniqueConstraint("parent_thread_id", "child_thread_id"),
        Index("ix_thread_edges_parent", "parent_thread_id"),
        Index("ix_thread_edges_source_id", "source_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("local_data_sources.id", ondelete="CASCADE"), nullable=False
    )
    parent_thread_id: Mapped[str] = mapped_column(String(255), nullable=False)
    child_thread_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ThreadGoal(Base, TimestampMixin):
    __tablename__ = "thread_goals"
    __table_args__ = (
        Index("ix_thread_goals_status_updated", "status", "updated_at_ms"),
        Index("ix_thread_goals_source_id", "source_id"),
    )

    thread_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    source_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("local_data_sources.id", ondelete="CASCADE"), nullable=False
    )
    goal_id: Mapped[str] = mapped_column(String(255), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    token_budget: Mapped[int | None] = mapped_column(BigInteger)
    tokens_used: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    time_used_seconds: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    source_created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class RetentionRun(Base, TimestampMixin):
    """One audited local-data cleanup request and its outcome.

    The row is created when an administrator requests a purge (before the
    Supervisor applies it) so the request survives a restart, then completed in
    place with the freed-byte accounting.
    """

    __tablename__ = "retention_runs"
    __table_args__ = (
        Index("ix_retention_runs_status_created", "status", "created_at"),
        Index("ix_retention_runs_requested_by", "requested_by", "created_at"),
        Index("ix_retention_runs_command_id", "command_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    command_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("system_commands.id", ondelete="SET NULL")
    )
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False, server_default="raw+index")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="pending"
    )
    keep_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="7")
    floor_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    selection: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    plan: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    result: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    bytes_freed: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    files_deleted: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    sessions_deleted: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_kind: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)


__all__ = [
    "Base",
    "metadata",
    "TimestampMixin",
    "User",
    "Role",
    "UserRole",
    "Session",
    "ApiToken",
    "AuditLog",
    "Workspace",
    "SystemCommand",
    "WorkerHeartbeat",
    "AgentSession",
    "IndexCheckpoint",
    "AgentEvent",
    "AgentTurn",
    "CodexMessage",
    "ToolCall",
    "LocalDataSource",
    "EvidenceRecord",
    "ActivitySpan",
    "SkillDefinition",
    "MemoryEntry",
    "SessionSkillEvidence",
    "RetentionRun",
    "ThreadEdge",
    "ThreadGoal",
]
