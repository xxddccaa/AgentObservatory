"""`agent-observatory user-passwd`: local password reset without the old one.

The command opens its own engine/session (like the other CLI entry points), so
these tests seed and assert through short-lived committing sessions on the CI
database instead of the rollback-wrapped `db_session` fixture; the CI database
is truncated by the `_test_engine` fixture before every test.
"""
from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from agent_observatory.cli import _run_user_passwd, main
from agent_observatory.db.models import ApiToken, AuditLog
from agent_observatory.db.models import Session as SessionModel
from agent_observatory.services import users


class _FakeTTY:
    def isatty(self) -> bool:
        return True


class _NoTTY:
    def isatty(self) -> bool:
        return False


@pytest.mark.anyio
async def test_reset_password_without_current_password(_test_engine) -> None:
    async with _test_engine() as setup:
        user = await users.create_user(
            setup, username="admin", password="old-password-123", roles=["admin"]
        )
        user_id = user.id
        await setup.commit()

    assert await _run_user_passwd(["admin", "--password", "brand-new-secret"]) == 0

    async with _test_engine() as check:
        assert await users.authenticate(check, "admin", "brand-new-secret") is not None
        assert await users.authenticate(check, "admin", "old-password-123") is None
        rows = (
            await check.scalars(select(AuditLog).where(AuditLog.action == "user.password_reset"))
        ).all()
        assert len(rows) == 1
        assert rows[0].actor_type == "bootstrap"
        assert rows[0].resource_id == user_id
        assert "brand-new-secret" not in str(rows[0].detail)


@pytest.mark.anyio
async def test_reset_revokes_only_target_users_credentials(_test_engine) -> None:
    now = datetime.now(UTC)
    async with _test_engine() as setup:
        target = await users.create_user(
            setup, username="admin", password="old-password-123", roles=["admin"]
        )
        other = await users.create_user(
            setup, username="other", password="other-password-123", roles=["viewer"]
        )
        setup.add_all(
            [
                SessionModel(
                    id="sess-admin",
                    user_id=target.id,
                    token_hash=os.urandom(32),
                    expires_at=now + timedelta(hours=1),
                ),
                SessionModel(
                    id="sess-other",
                    user_id=other.id,
                    token_hash=os.urandom(32),
                    expires_at=now + timedelta(hours=1),
                ),
                ApiToken(
                    id="tok-admin",
                    user_id=target.id,
                    name="cli",
                    token_hash=os.urandom(32),
                ),
                ApiToken(
                    id="tok-other",
                    user_id=other.id,
                    name="cli",
                    token_hash=os.urandom(32),
                ),
            ]
        )
        await setup.commit()

    assert await _run_user_passwd(["admin", "--password", "brand-new-secret"]) == 0

    async with _test_engine() as check:
        sessions = {row.id: row for row in (await check.scalars(select(SessionModel))).all()}
        tokens = {row.id: row for row in (await check.scalars(select(ApiToken))).all()}
    assert sessions["sess-admin"].revoked_at is not None
    assert sessions["sess-other"].revoked_at is None
    assert tokens["tok-admin"].revoked_at is not None
    assert tokens["tok-other"].revoked_at is None


@pytest.mark.anyio
async def test_unknown_user_exits_nonzero(_test_engine) -> None:
    assert await _run_user_passwd(["nobody", "--password", "whatever"]) == 1


@pytest.mark.anyio
async def test_prompt_requires_tty(_test_engine, monkeypatch) -> None:
    async with _test_engine() as setup:
        await users.create_user(
            setup, username="admin", password="old-password-123", roles=["admin"]
        )
        await setup.commit()

    monkeypatch.setattr(sys, "stdin", _NoTTY())
    assert await _run_user_passwd(["admin"]) == 1

    async with _test_engine() as check:
        assert await users.authenticate(check, "admin", "old-password-123") is not None


@pytest.mark.anyio
async def test_mismatched_prompts_do_not_change_password(_test_engine, monkeypatch) -> None:
    async with _test_engine() as setup:
        await users.create_user(
            setup, username="admin", password="old-password-123", roles=["admin"]
        )
        await setup.commit()

    answers = iter(["first-choice", "second-choice"])
    monkeypatch.setattr(sys, "stdin", _FakeTTY())
    monkeypatch.setattr("getpass.getpass", lambda *args, **kwargs: next(answers))
    assert await _run_user_passwd(["admin"]) == 1

    async with _test_engine() as check:
        assert await users.authenticate(check, "admin", "old-password-123") is not None


def test_cli_dispatch_wires_user_passwd() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["user-passwd", "--help"])
    assert excinfo.value.code == 0
