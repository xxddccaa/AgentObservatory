"""Backfill Codex session ids and parent-session relationships.

Revision ID: 0004_session_parent_link
Revises: 0003_workspace_activity
"""
from __future__ import annotations

from alembic import op

revision = "0004_session_parent_link"
down_revision = "0003_workspace_activity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Codex session_meta records use payload.id for the current rollout and
    # payload.session_id for the parent/root session when a subagent is spawned.
    # Older indexing populated every child thread_id from the parent id; repair
    # those rows from the already persisted raw event without rereading files.
    op.execute(
        """
        WITH meta AS (
            SELECT DISTINCT ON (agent_session_id)
                agent_session_id,
                raw_json->'payload'->>'id' AS current_id,
                raw_json->'payload'->>'session_id' AS parent_id
            FROM agent_events
            WHERE top_level_type = 'session_meta'
            ORDER BY agent_session_id, event_index
        )
        UPDATE agent_sessions AS s
        SET thread_id = COALESCE(meta.current_id, meta.parent_id, s.thread_id),
            parent_session_id = CASE
                WHEN meta.parent_id IS NOT NULL
                 AND meta.parent_id <> COALESCE(meta.current_id, '')
                    THEN meta.parent_id
                ELSE s.parent_session_id
            END,
            is_subagent = CASE
                WHEN meta.parent_id IS NOT NULL
                 AND meta.parent_id <> COALESCE(meta.current_id, '')
                    THEN TRUE
                ELSE s.is_subagent
            END
        FROM meta
        WHERE s.id = meta.agent_session_id
        """
    )


def downgrade() -> None:
    # Relationship repair is intentionally not reversed: the original parent
    # ids are not safely recoverable once a user has indexed new sessions.
    pass
