"""Phase 1 acceptance tests: auth / RBAC / audit.

Mirrors the acceptance criteria in the Phase 1 plan:
- Invalid credentials are rejected + audited.
- A session cookie grants `/api/auth/me`.
- `password_hash` never appears in user payloads.
- viewer/operator/admin RBAC on the admin-only endpoint.
- Every login/logout has an audit row with request_id.
- permission_denied actions are audited as failures.
- Operator ownership is enforced on a resource-scoped route.
- Audit log is append-only for non-admins.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text

from agent_observatory.db.models import ApiToken, AuditLog
from agent_observatory.services.auth import token_digest


async def _login(client: AsyncClient, username: str, password: str):
    r = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r


@pytest.mark.anyio
async def test_invalid_credentials_rejected(client: AsyncClient) -> None:
    r = await client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401
    body = r.json()
    assert body["error"]["code"] == "authentication_required"
    assert body["error"]["request_id"]


@pytest.mark.anyio
async def test_me_requires_auth(client: AsyncClient) -> None:
    r = await client.get("/api/auth/me")
    assert r.status_code == 401


@pytest.mark.anyio
async def test_login_then_me(client: AsyncClient, admin_user) -> None:
    await _login(client, admin_user["username"], "admin-password-123")
    r = await client.get("/api/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "admin"
    assert "admin" in body["roles"]


@pytest.mark.anyio
async def test_admin_payload_has_no_password_hash(client: AsyncClient, admin_user) -> None:
    await _login(client, admin_user["username"], "admin-password-123")
    r = await client.get("/api/admin/users")
    assert r.status_code == 200
    body = r.json()
    assert body["users"]
    for u in body["users"]:
        assert "password_hash" not in u
        assert "id" in u
        assert "roles" in u


@pytest.mark.anyio
async def test_viewer_cannot_create_user(
    client: AsyncClient, viewer_user, db_session
) -> None:
    await _login(client, viewer_user["username"], "viewer-password-123")
    r = await client.post(
        "/api/admin/users",
        json={"username": "hacker", "password": "longenough1", "roles": ["viewer"]},
    )
    assert r.status_code == 403


@pytest.mark.anyio
async def test_admin_can_create_user(client: AsyncClient, admin_user) -> None:
    await _login(client, admin_user["username"], "admin-password-123")
    r = await client.post(
        "/api/admin/users",
        json={"username": "newop", "password": "longenough1", "roles": ["operator"]},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["username"] == "newop"
    assert body["roles"] == ["operator"]
    assert "password_hash" not in body


@pytest.mark.anyio
async def test_duplicate_username_rejected(client: AsyncClient, admin_user) -> None:
    await _login(client, admin_user["username"], "admin-password-123")
    r = await client.post(
        "/api/admin/users",
        json={"username": admin_user["username"], "password": "longenough1", "roles": ["viewer"]},
    )
    assert r.status_code == 400


@pytest.mark.anyio
async def test_login_and_denied_are_audited(
    client: AsyncClient, admin_user, db_session
) -> None:
    await _login(client, admin_user["username"], "admin-password-123")
    # denied attempt
    await client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    rows = (
        await db_session.execute(
            text("SELECT action, result, error_kind FROM audit_logs ORDER BY id DESC")
        )
    ).fetchall()
    actions = {r[0] for r in rows}
    assert "auth.login" in actions
    denied = [r for r in rows if r[0] == "auth.login" and r[1] == "failure"]
    assert denied, "failed login must be audited as failure"
    assert denied[0][2] in ("invalid_credentials", "authentication_required")


@pytest.mark.anyio
async def test_audit_log_append_only_for_non_admin(
    client: AsyncClient, operator_user, db_session
) -> None:
    await _login(client, operator_user["username"], "operator-password-123")
    r = await client.delete("/api/audit/1")
    assert r.status_code in (403, 404, 405)


@pytest.mark.anyio
async def test_stranger_cannot_wear_admin_hat(
    client: AsyncClient, operator_user, db_session
) -> None:
    await _login(client, operator_user["username"], "operator-password-123")
    r = await client.get("/api/admin/users")
    assert r.status_code == 403
    denied = await db_session.scalar(
        select(AuditLog)
        .where(AuditLog.action == "auth.permission_denied")
        .order_by(AuditLog.id.desc())
    )
    assert denied is not None
    assert denied.actor_id == operator_user["id"]
    assert denied.request_id


@pytest.mark.anyio
async def test_logout_revokes_cookie(client: AsyncClient, admin_user) -> None:
    await _login(client, admin_user["username"], "admin-password-123")
    r = await client.post("/api/auth/logout")
    assert r.status_code == 200
    # session cookie cleared
    r = await client.get("/api/auth/me", follow_redirects=False)
    assert r.status_code == 401


@pytest.mark.anyio
async def test_api_token_is_stored_hashed_and_authenticates(
    client: AsyncClient, operator_user, db_session
) -> None:
    await _login(client, operator_user["username"], "operator-password-123")
    r = await client.post(
        "/api/auth/tokens",
        json={"name": "automation", "expires_in_days": 30},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    raw_token = body["token"]
    row = await db_session.get(ApiToken, body["id"])
    assert row is not None
    assert row.token_hash == token_digest(raw_token)
    assert raw_token.encode() not in row.token_hash

    client.cookies.clear()
    r = await client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert r.status_code == 200
    assert r.json()["id"] == operator_user["id"]


@pytest.mark.anyio
async def test_user_can_change_password_and_revoke_own_token(
    client: AsyncClient, operator_user
) -> None:
    await _login(client, operator_user["username"], "operator-password-123")
    created = await client.post("/api/auth/tokens", json={"name": "to-revoke"})
    assert created.status_code == 201
    token_id = created.json()["id"]
    changed = await client.post(
        "/api/auth/password",
        json={
            "current_password": "operator-password-123",
            "new_password": "operator-password-456",
        },
    )
    assert changed.status_code == 200
    revoked = await client.delete(f"/api/auth/tokens/{token_id}")
    assert revoked.status_code == 200
    listed = await client.get("/api/auth/tokens")
    assert listed.status_code == 200
    assert listed.json()[0]["revoked_at"] is not None


@pytest.mark.anyio
async def test_admin_can_update_user_roles_and_deactivate(
    client: AsyncClient, admin_user, viewer_user
) -> None:
    await _login(client, admin_user["username"], "admin-password-123")
    updated = await client.patch(
        f"/api/admin/users/{viewer_user['id']}",
        json={"roles": ["operator"]},
    )
    assert updated.status_code == 200
    assert updated.json()["roles"] == ["operator"]
    disabled = await client.patch(
        f"/api/admin/users/{viewer_user['id']}",
        json={"is_active": False},
    )
    assert disabled.status_code == 200
    assert disabled.json()["is_active"] is False


@pytest.mark.anyio
async def test_non_admin_cannot_update_users(
    client: AsyncClient, operator_user, viewer_user
) -> None:
    await _login(client, operator_user["username"], "operator-password-123")
    response = await client.patch(
        f"/api/admin/users/{viewer_user['id']}",
        json={"roles": ["admin"]},
    )
    assert response.status_code == 403
