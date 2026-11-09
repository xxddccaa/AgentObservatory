"""Track the projected event cursor and speed up per-session event lookups.

The scanner records how far a session's events have been folded into the
structured projection (messages / turns / tools) so an append that produced no
new events does not rebuild the whole projection.  The composite index makes
both the "highest projected index" and "events after cursor" lookups index-only.
"""
from __future__ import annotations

from alembic import op

revision = "0009_projection_cursor"
down_revision = "0008_memory_entries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE agent_sessions
            ADD COLUMN IF NOT EXISTS projected_event_index INTEGER NOT NULL DEFAULT -1
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_agent_events_session_event
            ON agent_events (agent_session_id, event_index)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_agent_events_session_event")
    op.execute("ALTER TABLE agent_sessions DROP COLUMN IF EXISTS projected_event_index")
