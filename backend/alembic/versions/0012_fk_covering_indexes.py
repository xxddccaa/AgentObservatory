"""Index the remaining foreign keys that drive delete cascades.

Deleting a parent row runs the foreign-key action once per deleted row, which
means an index lookup if the referencing column is indexed and a sequential
scan of the whole child table otherwise.  Projection rebuilds delete turns,
events and evidence for one session at a time, and local sources are deleted
with ``ON DELETE CASCADE``; every referencing column involved therefore needs
its own index.  Without these, a parser-version bump that resets a large
session degrades to scanning the big child tables once per deleted parent row.
"""
from __future__ import annotations

from alembic import op

revision = "0012_fk_covering_indexes"
down_revision = "0011_drop_duplicate_event_index"
branch_labels = None
depends_on = None

_INDEXES = (
    ("ix_messages_turn_id", "messages", "turn_id"),
    ("ix_tool_calls_turn_id", "tool_calls", "turn_id"),
    ("ix_evidence_records_agent_event_id", "evidence_records", "agent_event_id"),
    ("ix_session_skill_evidence_skill_id", "session_skill_evidence", "skill_id"),
    ("ix_thread_edges_source_id", "thread_edges", "source_id"),
    ("ix_thread_goals_source_id", "thread_goals", "source_id"),
    ("ix_user_roles_role_id", "user_roles", "role_id"),
)


def upgrade() -> None:
    for name, table, column in _INDEXES:
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({column})")


def downgrade() -> None:
    for name, _table, _column in reversed(_INDEXES):
        op.execute(f"DROP INDEX IF EXISTS {name}")
