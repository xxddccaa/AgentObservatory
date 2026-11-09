"""Alembic environment.

Runtime = asyncpg-backed async engine; migrations run synchronously on a
psycopg-style sync engine (strip `+asyncpg`). This mirrors resilient's
`db/migrations.py` and keeps one migration path for both `serve` and `worker`.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_observatory.config import settings  # noqa: E402
from agent_observatory.db import models  # noqa: E402

# this is the Alembic Config object, which provides access to the .ini file.
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = models.metadata


def _sync_url() -> str:
    """Alembic's sync API needs a psycopg-style URL; swap the async driver."""
    url = os.environ.get("TEST_DATABASE_URL") or str(settings().database_url)
    return url.replace("+asyncpg", "+psycopg")


def run_migrations_offline() -> None:
    url = _sync_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    config_section = config.get_section(config.config_ini_section, {})
    config_section["sqlalchemy.url"] = _sync_url()
    sync_engine = engine_from_config(
        config_section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with sync_engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata,
                          compare_type=True, compare_server_default=True)
        with context.begin_transaction():
            context.run_migrations()
    sync_engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
