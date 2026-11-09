"""Index the evidence foreign keys that make projection resets slow.

``activity_spans.evidence_id`` and ``session_skill_evidence.evidence_id``
reference ``evidence_records`` with ``ON DELETE SET NULL``.  Without an index
on the referencing column, every evidence row removed by a projection rebuild
makes PostgreSQL sequentially scan the whole referencing table, so resetting
one session's projection degraded into tens of seconds on a populated
database.  These indexes turn that into an index lookup per deleted row.
"""
from __future__ import annotations

from alembic import op

revision = "0010_evidence_fk_indexes"
down_revision = "0009_projection_cursor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_activity_spans_evidence_id
            ON activity_spans (evidence_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_session_skill_evidence_evidence_id
            ON session_skill_evidence (evidence_id)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_session_skill_evidence_evidence_id")
    op.execute("DROP INDEX IF EXISTS ix_activity_spans_evidence_id")
