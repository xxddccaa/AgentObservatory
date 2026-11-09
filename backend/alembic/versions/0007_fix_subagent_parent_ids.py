"""Correct parent ids for nested Claude subagent paths."""
from __future__ import annotations

from alembic import op

revision = "0007_fix_subagent_parent_ids"
down_revision = "0006_nested_subagent_paths"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE agent_sessions
        SET parent_session_id = regexp_replace(
                source_path,
                '^.*/([^/]+)/subagents/.*$',
                E'\\\\1'
            ),
            is_subagent = TRUE
        WHERE source_kind = 'claude_jsonl'
          AND source_path ~ '/subagents/'
        """
    )


def downgrade() -> None:
    pass
