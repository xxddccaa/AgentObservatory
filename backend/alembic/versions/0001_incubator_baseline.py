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
    bind = op.get_bind()
    metadata.create_all(bind=bind, checkfirst=False)
    bind.exec_driver_sql(
        "INSERT INTO roles (name) VALUES ('admin'), ('operator'), ('viewer')"
    )


def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind(), checkfirst=True)
