from __future__ import annotations

import base64
import io
import zipfile
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from agent_observatory.db.models import AuditLog, LocalDataSource, MemoryEntry


async def _seed_memory(
    db_session,
    *,
    entry_id: str = "memory-one",
    sensitive: bool = True,
) -> MemoryEntry:
    source = LocalDataSource(
        id="memory-source",
        agent="claude",
        source_kind="claude_memory_files",
        source_identity="claude-memory:/tmp/claude",
        source_path="/tmp/claude",
        source_format="directory",
        parser_version="1",
        cursor_json={},
        fingerprint_json={},
        status="indexed",
        record_count=1,
    )
    row = MemoryEntry(
        id=entry_id,
        source_id=source.id,
        agent="claude",
        source_kind=source.source_kind,
        source_record_id="project/memory/topic.md",
        source_path="/tmp/claude/project/memory/topic.md",
        scope="project",
        title="Deployment notes",
        content=(
            "api_key=super-secret-value"
            if sensitive
            else "Run the audited restart command."
        ),
        summary=(
            "api_key=super-secret-value"
            if sensitive
            else "Audited deployment notes"
        ),
        content_hash="hash-one",
        source_updated_at=datetime(2026, 9, 1, tzinfo=UTC),
        last_seen_at=datetime(2026, 9, 2, tzinfo=UTC),
        is_present=True,
        sensitivity="sensitive" if sensitive else "normal",
        metadata_json={"project_key": "project", "is_index": False},
    )
    db_session.add_all([source, row])
    await db_session.flush()
    return row


@pytest.mark.anyio
async def test_memory_list_and_detail_redact_sensitive_content(
    as_viewer, db_session
) -> None:
    row = await _seed_memory(db_session)
    response = await as_viewer.get("/api/memories")
    assert response.status_code == 200
    item = response.json()["entries"][0]
    assert item["id"] == row.id
    assert "super-secret-value" not in item["summary"]
    assert item["sensitivity"] == "sensitive"

    detail = await as_viewer.get(f"/api/memories/{row.id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["content_redacted"] is True
    assert "super-secret-value" not in body["content"]
    log = await db_session.scalar(
        select(AuditLog).where(AuditLog.action == "memory.viewed")
    )
    assert log is not None
    assert "content" not in log.detail


@pytest.mark.anyio
async def test_full_content_requires_operator_and_is_audited(
    client, as_viewer, operator_user, db_session
) -> None:
    row = await _seed_memory(db_session)
    denied = await as_viewer.post(
        f"/api/memories/{row.id}/content",
        json={"format": "raw", "purpose": "copy"},
    )
    assert denied.status_code == 403

    await client.post(
        "/api/auth/login",
        json={
            "username": operator_user["username"],
            "password": "operator-password-123",
        },
    )
    copied = await client.post(
        f"/api/memories/{row.id}/content",
        json={"format": "raw", "purpose": "copy"},
    )
    assert copied.status_code == 200
    assert copied.json()["content"] == "api_key=super-secret-value"
    log = await db_session.scalar(
        select(AuditLog).where(AuditLog.action == "memory.copied")
    )
    assert log is not None
    assert log.detail["content_hash"] == "hash-one"
    assert "super-secret-value" not in str(log.detail)


@pytest.mark.anyio
async def test_operator_can_export_portable_zip(
    as_operator, db_session
) -> None:
    row = await _seed_memory(db_session, sensitive=False)
    response = await as_operator.post(
        "/api/memories/export",
        json={"ids": [row.id], "format": "zip", "target": "claude"},
    )
    assert response.status_code == 200
    body = response.json()
    archive_bytes = base64.b64decode(body["content"])
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        assert {"manifest.json", "memories.md"}.issubset(archive.namelist())
        manifest = archive.read("manifest.json").decode()
        assert '"target_cli": "claude"' in manifest
        assert "super-secret-value" not in manifest
    log = await db_session.scalar(
        select(AuditLog).where(AuditLog.action == "memory.exported")
    )
    assert log is not None
    assert log.detail["entry_count"] == 1


@pytest.mark.anyio
async def test_memory_sources_include_unsupported_agents(
    as_viewer, db_session
) -> None:
    await _seed_memory(db_session, sensitive=False)
    response = await as_viewer.get("/api/memories/sources")
    assert response.status_code == 200
    sources = {item["agent"]: item for item in response.json()["sources"]}
    assert sources["claude"]["status"] == "indexed"
    assert sources["dsh"]["status"] == "unsupported"
    assert sources["opencode"]["record_count"] == 0
