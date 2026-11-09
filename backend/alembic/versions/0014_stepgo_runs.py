"""Persist StepGo host runs and their native-session links.

Revision ID: 0014_stepgo_runs
Revises: 0013_retention_runs
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0014_stepgo_runs"
down_revision = "0013_retention_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("stepgo_runs"):
        op.create_table(
            "stepgo_runs",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("host_run_id", sa.String(length=255), nullable=False),
            sa.Column("source_identity", sa.Text(), nullable=False),
            sa.Column("source_path", sa.Text(), nullable=False),
            sa.Column("agent", sa.String(length=32), server_default="unknown", nullable=False),
            sa.Column("module", sa.String(length=255), nullable=True),
            sa.Column("status", sa.String(length=32), server_default="unknown", nullable=False),
            sa.Column("link_status", sa.String(length=32), server_default="unlinked", nullable=False),
            sa.Column("link_reason", sa.String(length=64), nullable=True),
            sa.Column("linked_agent_session_id", sa.String(length=64), nullable=True),
            sa.Column("native_session_id", sa.String(length=255), nullable=True),
            sa.Column("transcript_path", sa.Text(), nullable=True),
            sa.Column("start_time", sa.DateTime(timezone=True), nullable=True),
            sa.Column("end_time", sa.DateTime(timezone=True), nullable=True),
            sa.Column("exit_code", sa.Integer(), nullable=True),
            sa.Column(
                "metrics_json",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=sa.text("'{}'::jsonb"),
                nullable=False,
            ),
            sa.Column(
                "raw_start_json",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=sa.text("'{}'::jsonb"),
                nullable=False,
            ),
            sa.Column(
                "raw_end_json",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=sa.text("'{}'::jsonb"),
                nullable=False,
            ),
            sa.Column("file_size", sa.BigInteger(), server_default="0", nullable=False),
            sa.Column("file_mtime_ns", sa.BigInteger(), server_default="0", nullable=False),
            sa.Column("file_sha256", sa.String(length=64), nullable=True),
            sa.Column(
                "last_indexed_offset", sa.BigInteger(), server_default="0", nullable=False
            ),
            sa.Column(
                "last_indexed_line", sa.BigInteger(), server_default="0", nullable=False
            ),
            sa.Column("event_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column(
                "index_status", sa.String(length=32), server_default="discovered", nullable=False
            ),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["linked_agent_session_id"],
                ["agent_sessions.id"],
                name="fk_stepgo_runs_linked_agent_session_id_agent_sessions",
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_stepgo_runs"),
            sa.UniqueConstraint("source_identity", name="uq_stepgo_runs_source_identity"),
        )
        op.create_index("ix_stepgo_runs_host_run_id", "stepgo_runs", ["host_run_id"])
        op.create_index(
            "ix_stepgo_runs_agent_session",
            "stepgo_runs",
            ["linked_agent_session_id", "start_time"],
        )
        op.create_index(
            "ix_stepgo_runs_link_status", "stepgo_runs", ["link_status", "updated_at"]
        )
        op.create_index("ix_stepgo_runs_source_path", "stepgo_runs", ["source_path"])

    if not inspector.has_table("stepgo_run_events"):
        op.create_table(
            "stepgo_run_events",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("run_id", sa.String(length=64), nullable=False),
            sa.Column("event_index", sa.Integer(), nullable=False),
            sa.Column("line_number", sa.BigInteger(), nullable=False),
            sa.Column("byte_offset", sa.BigInteger(), nullable=False),
            sa.Column("event_type", sa.String(length=64), nullable=False),
            sa.Column("event_timestamp", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "raw_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False
            ),
            sa.Column("raw_hash", sa.String(length=64), nullable=False),
            sa.Column("is_known", sa.Boolean(), server_default="false", nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["run_id"],
                ["stepgo_runs.id"],
                name="fk_stepgo_run_events_run_id_stepgo_runs",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_stepgo_run_events"),
            sa.UniqueConstraint(
                "run_id", "event_index", name="uq_stepgo_run_events_run_id"
            ),
        )
        op.create_index(
            "ix_stepgo_run_events_run_line",
            "stepgo_run_events",
            ["run_id", "line_number"],
        )


def downgrade() -> None:
    op.drop_table("stepgo_run_events")
    op.drop_table("stepgo_runs")
