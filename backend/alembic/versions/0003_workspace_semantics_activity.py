"""Persist workspace semantics and session activity timestamps.

Revision ID: 0003_workspace_activity
Revises: 0002_project_activity_idx
"""
from __future__ import annotations

from alembic import op

revision = "0003_workspace_activity"
down_revision = "0002_project_activity_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE workspaces
            ADD COLUMN IF NOT EXISTS workspace_kind VARCHAR(32) NOT NULL DEFAULT 'unknown',
            ADD COLUMN IF NOT EXISTS workspace_confidence VARCHAR(16) NOT NULL DEFAULT 'low'
        """
    )
    op.execute(
        """
        ALTER TABLE agent_sessions
            ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMPTZ
        """
    )
    op.execute(
        """
        UPDATE agent_sessions
        SET last_activity_at = COALESCE(end_time, start_time)
        WHERE last_activity_at IS NULL
        """
    )
    # Backfill classifications that are deterministic from the persisted path
    # alone.  Git checkout detection remains an indexer concern because
    # migrations must not access the host filesystem; the worker reclassifies
    # those rows on its normal scan.
    op.execute(
        """
        UPDATE workspaces
        SET workspace_kind = CASE
                WHEN path IN ('/', '/bin', '/boot', '/dev', '/etc', '/home',
                              '/lib', '/lib64', '/opt', '/proc', '/root',
                              '/run', '/sbin', '/srv', '/sys', '/tmp',
                              '/usr', '/var')
                    THEN 'system_directory'
                WHEN path LIKE '/tmp/pytest-%'
                  OR path LIKE '%/pytest-of-%/%'
                  OR path LIKE '%/tmppytest-%'
                    THEN 'test_fixture'
                WHEN path LIKE '/tmp/tmp%'
                  OR path LIKE '/tmp/rcw-%'
                  OR path LIKE '/tmp/task-%'
                  OR path LIKE '%/rcw-%/%'
                    THEN 'ephemeral_task'
                ELSE workspace_kind
            END,
            workspace_confidence = CASE
                WHEN path IN ('/', '/bin', '/boot', '/dev', '/etc', '/home',
                              '/lib', '/lib64', '/opt', '/proc', '/root',
                              '/run', '/sbin', '/srv', '/sys', '/tmp',
                              '/usr', '/var')
                  OR path LIKE '/tmp/pytest-%'
                  OR path LIKE '%/pytest-of-%/%'
                  OR path LIKE '%/tmppytest-%'
                  OR path LIKE '/tmp/tmp%'
                  OR path LIKE '/tmp/rcw-%'
                  OR path LIKE '/tmp/task-%'
                  OR path LIKE '%/rcw-%/%'
                    THEN 'high'
                ELSE workspace_confidence
            END
        WHERE workspace_kind = 'unknown' OR workspace_confidence = 'low'
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agent_sessions DROP COLUMN IF EXISTS last_activity_at")
    op.execute("ALTER TABLE workspaces DROP COLUMN IF EXISTS workspace_confidence")
    op.execute("ALTER TABLE workspaces DROP COLUMN IF EXISTS workspace_kind")
