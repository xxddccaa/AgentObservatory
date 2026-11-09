"""Persist bounded StepGo fingerprints and parser versions."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0015_stepgo_fingerprints"
down_revision = "0014_stepgo_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("stepgo_runs")}
    if "parser_version" not in columns:
        op.add_column(
            "stepgo_runs",
            sa.Column("parser_version", sa.String(32), nullable=False, server_default="1"),
        )
    if "fingerprint_json" not in columns:
        op.add_column(
            "stepgo_runs",
            sa.Column(
                "fingerprint_json", postgresql.JSONB(), nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
        )


def downgrade() -> None:
    op.drop_column("stepgo_runs", "fingerprint_json")
    op.drop_column("stepgo_runs", "parser_version")
