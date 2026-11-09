"""Drop the redundant event index that duplicates a unique constraint.

``ix_agent_events_session_event`` covers exactly the same columns, in the same
order, as the unique constraint ``uq_agent_events_agent_session_id`` on
``agent_events (agent_session_id, event_index)``.  Keeping both only adds
write cost to the largest, hottest table, so the plain index is removed; the
projection cursor's ``max(event_index)`` lookup is still served by the unique
index (index-only scan).
"""
from __future__ import annotations

from alembic import op

revision = "0011_drop_duplicate_event_index"
down_revision = "0010_evidence_fk_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_agent_events_session_event")


def downgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_agent_events_session_event
            ON agent_events (agent_session_id, event_index)
        """
    )
