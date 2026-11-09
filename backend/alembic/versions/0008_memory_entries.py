"""Index read-only CLI memory entries."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_memory_entries"
down_revision = "0007_fix_subagent_parent_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # The incubator baseline creates tables from current metadata on a fresh
    # database. In that case this table already exists before revision 0008;
    # retain the revision marker without attempting a duplicate CREATE TABLE.
    if inspector.has_table("memory_entries"):
        return
    op.create_table(
        "memory_entries",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=True),
        sa.Column("agent", sa.String(length=32), nullable=False),
        sa.Column("source_kind", sa.String(length=64), nullable=False),
        sa.Column("source_record_id", sa.String(length=255), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.String(length=255), nullable=True),
        sa.Column(
            "scope", sa.String(length=32), server_default="project", nullable=False
        ),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "is_present", sa.Boolean(), server_default="true", nullable=False
        ),
        sa.Column(
            "sensitivity",
            sa.String(length=16),
            server_default="normal",
            nullable=False,
        ),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
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
            ["source_id"],
            ["local_data_sources.id"],
            name=op.f("fk_memory_entries_source_id_local_data_sources"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_memory_entries_workspace_id_workspaces"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_entries")),
        sa.UniqueConstraint(
            "source_id",
            "source_record_id",
            name=op.f("uq_memory_entries_source_id"),
        ),
    )
    op.create_index(
        "ix_memory_entries_agent_present",
        "memory_entries",
        ["agent", "is_present"],
        unique=False,
    )
    op.create_index(
        "ix_memory_entries_source_updated",
        "memory_entries",
        ["source_updated_at"],
        unique=False,
    )
    op.create_index(
        "ix_memory_entries_workspace_present",
        "memory_entries",
        ["workspace_id", "is_present"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_memory_entries_workspace_present", table_name="memory_entries")
    op.drop_index("ix_memory_entries_source_updated", table_name="memory_entries")
    op.drop_index("ix_memory_entries_agent_present", table_name="memory_entries")
    op.drop_table("memory_entries")
