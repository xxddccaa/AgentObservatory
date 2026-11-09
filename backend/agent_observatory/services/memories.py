"""Queries and portable exports for indexed CLI memory entries."""
from __future__ import annotations

import base64
import io
import json
import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_observatory.db.models import (
    LocalDataSource,
    MemoryEntry,
    Workspace,
)
from agent_observatory.services.redaction import redact_secrets

MEMORY_SOURCE_KINDS = {
    "codex_memories_sqlite",
    "stepcode_memories_sqlite",
    "claude_memory_files",
}
KNOWN_MEMORY_AGENTS = ("codex", "stepcode", "claude", "dsh", "opencode")
EXPORT_FORMATS = {"markdown", "zip"}
EXPORT_TARGETS = {"generic", "codex", "stepcode", "claude"}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _activity(row: MemoryEntry) -> datetime:
    return row.source_updated_at or row.updated_at


def _encode_cursor(row: MemoryEntry) -> str:
    raw = json.dumps([_activity(row).isoformat(), row.id]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(value: str) -> tuple[datetime, str]:
    padded = value + "=" * (-len(value) % 4)
    timestamp, entry_id = json.loads(
        base64.urlsafe_b64decode(padded.encode()).decode()
    )
    parsed = datetime.fromisoformat(str(timestamp))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC), str(entry_id)


def _redacted(value: str | None) -> str | None:
    return redact_secrets(value)


def serialize_summary(
    row: MemoryEntry,
    workspace: Workspace | None,
) -> dict[str, Any]:
    return {
        "id": row.id,
        "agent": row.agent,
        "source_kind": row.source_kind,
        "source_path": row.source_path,
        "thread_id": row.thread_id,
        "scope": row.scope,
        "title": row.title,
        "summary": _redacted(row.summary),
        "content_bytes": len(row.content.encode("utf-8", errors="replace")),
        "source_updated_at": _iso(row.source_updated_at),
        "last_seen_at": _iso(row.last_seen_at),
        "is_present": row.is_present,
        "sensitivity": row.sensitivity,
        "workspace": (
            {
                "id": workspace.id,
                "name": workspace.display_name,
                "path": workspace.path,
            }
            if workspace
            else None
        ),
        "project_key": row.metadata_json.get("project_key"),
        "is_index": bool(row.metadata_json.get("is_index")),
    }


async def list_entries(
    db: AsyncSession,
    *,
    limit: int,
    cursor: str | None,
    query: str | None,
    agent: str | None,
    scope: str | None,
    workspace_id: str | None,
    include_missing: bool,
) -> dict[str, Any]:
    stmt = select(MemoryEntry, Workspace).outerjoin(
        Workspace, Workspace.id == MemoryEntry.workspace_id
    )
    if not include_missing:
        stmt = stmt.where(MemoryEntry.is_present.is_(True))
    if agent:
        stmt = stmt.where(MemoryEntry.agent == agent)
    if scope:
        stmt = stmt.where(MemoryEntry.scope == scope)
    if workspace_id:
        stmt = stmt.where(MemoryEntry.workspace_id == workspace_id)
    search = (query or "").strip()
    if search:
        escaped = search.replace("%", r"\%").replace("_", r"\_")
        pattern = f"%{escaped}%"
        stmt = stmt.where(
            or_(
                MemoryEntry.title.ilike(pattern, escape="\\"),
                MemoryEntry.summary.ilike(pattern, escape="\\"),
                MemoryEntry.content.ilike(pattern, escape="\\"),
                MemoryEntry.source_path.ilike(pattern, escape="\\"),
            )
        )
    activity = func.coalesce(MemoryEntry.source_updated_at, MemoryEntry.updated_at)
    if cursor:
        cursor_time, cursor_id = _decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                activity < cursor_time,
                and_(activity == cursor_time, MemoryEntry.id < cursor_id),
            )
        )
    rows = list(
        (
            await db.execute(
                stmt.order_by(activity.desc(), MemoryEntry.id.desc()).limit(limit + 1)
            )
        ).all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "entries": [serialize_summary(row, workspace) for row, workspace in rows],
        "next_cursor": (
            _encode_cursor(rows[-1][0]) if has_more and rows else None
        ),
    }


async def get_entry(db: AsyncSession, entry_id: str) -> MemoryEntry | None:
    return await db.get(MemoryEntry, entry_id)


async def get_workspace(
    db: AsyncSession, workspace_id: str | None
) -> Workspace | None:
    return await db.get(Workspace, workspace_id) if workspace_id else None


def serialize_detail(
    row: MemoryEntry,
    workspace: Workspace | None,
    *,
    reveal: bool,
) -> dict[str, Any]:
    result = serialize_summary(row, workspace)
    is_redacted = row.sensitivity == "sensitive" and not reveal
    result.update(
        {
            "content": _redacted(row.content) if is_redacted else row.content,
            "content_redacted": is_redacted,
            "content_hash": row.content_hash,
            "source_record_id": row.source_record_id,
            "metadata": row.metadata_json,
        }
    )
    return result


async def source_status(db: AsyncSession) -> dict[str, Any]:
    rows = list(
        (
            await db.scalars(
                select(LocalDataSource).where(
                    LocalDataSource.source_kind.in_(MEMORY_SOURCE_KINDS)
                )
            )
        ).all()
    )
    by_agent = {row.agent: row for row in rows}
    return {
        "sources": [
            {
                "agent": agent,
                "source_kind": (
                    by_agent[agent].source_kind if agent in by_agent else None
                ),
                "status": by_agent[agent].status if agent in by_agent else "unsupported",
                "source_path": (
                    by_agent[agent].source_path if agent in by_agent else None
                ),
                "record_count": (
                    int(by_agent[agent].record_count) if agent in by_agent else 0
                ),
                "last_success_at": (
                    _iso(by_agent[agent].last_success_at)
                    if agent in by_agent
                    else None
                ),
            }
            for agent in KNOWN_MEMORY_AGENTS
        ]
    }


def render_markdown(entries: list[MemoryEntry], *, target: str) -> str:
    parts = [
        "# Agent Observatory memory export",
        "",
        f"Target CLI: {target}",
        f"Generated at: {datetime.now(UTC).isoformat()}",
    ]
    for entry in entries:
        parts.extend(
            [
                "",
                "---",
                "",
                f"## {entry.title}",
                "",
                f"- Source CLI: `{entry.agent}`",
                f"- Scope: `{entry.scope}`",
                f"- Source: `{entry.source_path}`",
                f"- Content SHA-256: `{entry.content_hash}`",
                "",
                entry.content,
            ]
        )
    return "\n".join(parts).rstrip() + "\n"


def _safe_segment(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return normalized[:120] or "memory"


def build_export(
    entries: list[MemoryEntry],
    *,
    format_name: str,
    target: str,
) -> dict[str, Any]:
    if format_name not in EXPORT_FORMATS:
        raise ValueError("format must be one of markdown|zip")
    if target not in EXPORT_TARGETS:
        raise ValueError("target must be one of generic|codex|stepcode|claude")
    markdown = render_markdown(entries, target=target)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    if format_name == "markdown":
        return {
            "filename": f"agent-observatory-memories-{stamp}.md",
            "media_type": "text/markdown",
            "encoding": "utf-8",
            "content": markdown,
        }

    manifest_entries = []
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("memories.md", markdown)
        for index, entry in enumerate(entries, start=1):
            source_name = Path(entry.source_path).name
            suffix = Path(source_name).suffix.lower()
            if suffix not in _TEXT_SUFFIXES:
                suffix = ".md"
            filename = (
                f"entries/{_safe_segment(entry.agent)}/"
                f"{index:03d}-{_safe_segment(entry.title)}{suffix}"
            )
            archive.writestr(filename, entry.content)
            manifest_entries.append(
                {
                    "id": entry.id,
                    "agent": entry.agent,
                    "scope": entry.scope,
                    "thread_id": entry.thread_id,
                    "title": entry.title,
                    "source_path": entry.source_path,
                    "content_hash": entry.content_hash,
                    "archive_path": filename,
                }
            )
        manifest = {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "target_cli": target,
            "entry_count": len(entries),
            "entries": manifest_entries,
        }
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
    return {
        "filename": f"agent-observatory-memories-{stamp}.zip",
        "media_type": "application/zip",
        "encoding": "base64",
        "content": base64.b64encode(buffer.getvalue()).decode(),
    }


_TEXT_SUFFIXES = {".md", ".txt"}

__all__ = [
    "EXPORT_FORMATS",
    "EXPORT_TARGETS",
    "build_export",
    "get_entry",
    "get_workspace",
    "list_entries",
    "serialize_detail",
    "source_status",
]
