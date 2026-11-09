"""Handle Claude nested ``subagents/workflows/...`` paths."""
from __future__ import annotations

from alembic import op

revision = "0006_nested_subagent_paths"
down_revision = "0005_claude_subagent_paths"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE agent_sessions
        SET parent_session_id = regexp_replace(
                source_path,
                '^.*/([^/]+)/subagents/.*$',
                '\\\\1'
            ),
            is_subagent = TRUE
        WHERE source_kind = 'claude_jsonl'
          AND source_path ~ '/subagents/'
          AND COALESCE(parent_session_id, '') = ''
        """
    )


def downgrade() -> None:
    pass
