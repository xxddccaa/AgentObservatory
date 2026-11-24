"""Drop accounts, roles, login sessions and API tokens.

The observatory is a single-user tool bound to loopback by default; access
control is the network binding plus the Host / Origin checks in ``main.py``.
Audit rows keep their historical ``actor_id`` values, but the column no longer
references ``users``.  New rows are written with ``actor_type = 'local'``.
"""
from __future__ import annotations

from alembic import op

revision = "0016_drop_accounts"
down_revision = "0015_stepgo_fingerprints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE audit_logs DROP CONSTRAINT IF EXISTS fk_audit_logs_actor_id_users"
    )
    op.execute("ALTER TABLE audit_logs ALTER COLUMN actor_type SET DEFAULT 'local'")
    op.execute("DROP TABLE IF EXISTS api_tokens, sessions, user_roles, roles, users CASCADE")


def downgrade() -> None:
    """Recreate the (empty) account schema of 0015 so older code can start.

    Historical ``actor_id`` values cannot reference the recreated, empty
    ``users`` table, so they are cleared before the foreign key returns.
    """
    op.execute(
        """
        CREATE TABLE users (
            id VARCHAR(64) NOT NULL,
            username VARCHAR(255) NOT NULL,
            password_hash TEXT NOT NULL,
            is_active BOOLEAN DEFAULT true NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_users PRIMARY KEY (id),
            CONSTRAINT uq_users_username UNIQUE (username)
        );
        CREATE TABLE roles (
            id SERIAL NOT NULL,
            name VARCHAR(32) NOT NULL,
            CONSTRAINT pk_roles PRIMARY KEY (id),
            CONSTRAINT uq_roles_name UNIQUE (name)
        );
        CREATE TABLE user_roles (
            user_id VARCHAR(64) NOT NULL,
            role_id INTEGER NOT NULL,
            CONSTRAINT pk_user_roles PRIMARY KEY (user_id, role_id),
            CONSTRAINT fk_user_roles_user_id_users FOREIGN KEY (user_id)
                REFERENCES users (id) ON DELETE CASCADE,
            CONSTRAINT fk_user_roles_role_id_roles FOREIGN KEY (role_id)
                REFERENCES roles (id) ON DELETE CASCADE
        );
        CREATE INDEX ix_user_roles_role_id ON user_roles (role_id);
        CREATE TABLE sessions (
            id VARCHAR(64) NOT NULL,
            user_id VARCHAR(64) NOT NULL,
            token_hash BYTEA NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
            revoked_at TIMESTAMP WITH TIME ZONE,
            ip_address VARCHAR(64),
            user_agent TEXT,
            CONSTRAINT pk_sessions PRIMARY KEY (id),
            CONSTRAINT uq_sessions_token_hash UNIQUE (token_hash),
            CONSTRAINT fk_sessions_user_id_users FOREIGN KEY (user_id)
                REFERENCES users (id) ON DELETE CASCADE
        );
        CREATE INDEX ix_sessions_user_id ON sessions (user_id);
        CREATE TABLE api_tokens (
            id VARCHAR(64) NOT NULL,
            user_id VARCHAR(64) NOT NULL,
            name VARCHAR(255) NOT NULL,
            token_hash BYTEA NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            expires_at TIMESTAMP WITH TIME ZONE,
            last_used_at TIMESTAMP WITH TIME ZONE,
            revoked_at TIMESTAMP WITH TIME ZONE,
            CONSTRAINT pk_api_tokens PRIMARY KEY (id),
            CONSTRAINT uq_api_tokens_token_hash UNIQUE (token_hash),
            CONSTRAINT fk_api_tokens_user_id_users FOREIGN KEY (user_id)
                REFERENCES users (id) ON DELETE CASCADE
        );
        CREATE INDEX ix_api_tokens_user_id ON api_tokens (user_id);
        INSERT INTO roles (name) VALUES ('admin'), ('operator'), ('viewer');
        UPDATE audit_logs SET actor_id = NULL WHERE actor_id IS NOT NULL;
        ALTER TABLE audit_logs ALTER COLUMN actor_type SET DEFAULT 'user';
        ALTER TABLE audit_logs ADD CONSTRAINT fk_audit_logs_actor_id_users
            FOREIGN KEY (actor_id) REFERENCES users (id) ON DELETE SET NULL;
        """
    )
