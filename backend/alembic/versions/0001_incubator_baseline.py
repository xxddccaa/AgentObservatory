"""Agent Observatory incubator baseline.

The project is not yet deployed. This single baseline intentionally replaces
the inherited Codex migration chain and creates the neutral multi-agent schema
directly from the reviewed SQLAlchemy metadata.
"""
from __future__ import annotations

from alembic import op

from agent_observatory.db.models import metadata

revision = "0001_incubator_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The account tables (users / roles / sessions / api_tokens) left the
    # metadata with 0016; a fresh database never creates them.
    metadata.create_all(bind=op.get_bind(), checkfirst=False)


def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind(), checkfirst=True)
    # Left behind by 0016's downgrade on databases that once had accounts.
    op.execute("DROP TABLE IF EXISTS api_tokens, sessions, user_roles, roles, users CASCADE")
