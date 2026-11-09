"""Record audited local-data cleanup runs.

The Storage page plans a purge against the local agent roots, the Supervisor
applies it, and this table keeps the request, the previewed plan and the
freed-byte accounting so a cleanup is explainable after the fact.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013_retention_runs"
down_revision = "0012_fk_covering_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("retention_runs"):
        return
    op.create_table(
        "retention_runs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("command_id", sa.BigInteger(), nullable=True),
        sa.Column("requested_by", sa.String(length=255), nullable=False),
        sa.Column(
            "scope",
            sa.String(length=32),
            server_default="raw+index",
            nullable=False,
        ),
        sa.Column(
            "status", sa.String(length=32), server_default="pending", nullable=False
        ),
        sa.Column("keep_days", sa.Integer(), server_default="7", nullable=False),
        sa.Column("floor_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "selection",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "plan",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "result",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("bytes_freed", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("files_deleted", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "sessions_deleted", sa.BigInteger(), server_default="0", nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_kind", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
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
            ["command_id"],
            ["system_commands.id"],
            name=op.f("fk_retention_runs_command_id_system_commands"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_retention_runs")),
    )
    op.create_index(
        "ix_retention_runs_status_created",
        "retention_runs",
        ["status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_retention_runs_requested_by",
        "retention_runs",
        ["requested_by", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_retention_runs_command_id",
        "retention_runs",
        ["command_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_retention_runs_requested_by", table_name="retention_runs")
    op.drop_index("ix_retention_runs_command_id", table_name="retention_runs")
    op.drop_index("ix_retention_runs_status_created", table_name="retention_runs")
    op.drop_table("retention_runs")
