"""Index turn activity time for project token rollups.

Revision ID: 0002_project_activity_idx
Revises: 0001_incubator_baseline
"""
from __future__ import annotations

from alembic import op

revision = "0002_project_activity_idx"
down_revision = "0001_incubator_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_agent_turns_activity_time
        ON agent_turns (coalesce(completed_at, started_at, created_at))
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_agent_turns_activity_time")
