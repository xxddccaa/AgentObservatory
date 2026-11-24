"""Gemini CLI (``@google/gemini-cli``) session adapter.

Gemini CLI stores workspace mappings in ``<gemini_home>/projects.json`` and
per-project files in ``<gemini_home>/tmp/<slug>/``.  Conversations live inside
``<slug>/chats/``:

- Main sessions: ``session-*.jsonl`` (event-sourced JSONL) or legacy
  ``session-*.json`` (single JSON snapshot).
- Subagent sessions: ``<parentSessionId>/<subagentSessionId>.jsonl`` with
  ``kind="subagent"`` metadata and parent linkage via the directory name.

The JSONL stream is event-sourced by ``ChatRecordingService``:

- Header line: ``{sessionId, projectHash, startTime, lastUpdated, kind, ...}``.
- Incremental updates append complete snapshots sharing the same message
  ``id`` as thoughts, tokens, scheduled tool calls, and finished tool calls
  arrive.
- ``{"$set": {...}}`` updates metadata (``summary``, ``lastUpdated``,
  ``directories``) or replaces the active ``messages`` array.
- ``{"$patch": {...}}`` patches existing message contents/tool results, deletes
  ``removeIds``, or reorders ``orderIds``.
- ``{"$rewindTo": "<msg_id>"}`` truncates history from ``<msg_id>`` onward.

All events (including oversized lines and unparseable records) are preserved in
``agent_events`` with bounded memory, while conversation projection replays
the ordered ``messages_map`` by message ``id`` so incremental snapshots never
duplicate messages, turns, tools, or tokens.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_observatory.db.models import (
    ActivitySpan,
    AgentEvent,
    AgentSession,
    AgentTurn,
    CodexMessage,
    ToolCall,
)

from ..tree_cache import cached_resolve, full_scan_requested
from .base import PlanAction, SourceDescriptor, SourcePlan
from .common import (
    MAX_LINE_BYTES,
    ONGOING_SECONDS,
    as_int,
    clear_session_rows,
    duration_ms,
    iter_raw_jsonl_lines,
    parse_ts,
    pg_safe,
    plan_append_jsonl,
    plan_stat_rebuild,
    stable_id,
)

_PREFIX_BYTES = 4096
_EVENT_BATCH = 250
_MAX_LEGACY_JSON_BYTES = 64 * 1024 * 1024

_KNOWN_MESSAGE_TYPES = frozenset({"user", "gemini", "info", "error", "warning"})
_NORMALIZED_TYPES = {
    "session_meta": "session_meta",
    "meta_set": "session_update",
    "patch": "message_patch",
    "rewind": "history_rewind",
    "user": "user_message",
    "gemini": "assistant_response",
    "info": "system_message",
    "warning": "system_message",
    "error": "error_message",
    "parse_error": "parse_error",
}
_TOOL_KINDS = {
    "read_file": "read",
    "read_many_files": "read",
    "list_directory": "read",
    "get_internal_docs": "read",
    "read_mcp_resource": "read",
    "list_mcp_resources": "read",
    "glob": "search",
    "grep_search": "search",
    "write_file": "write",
    "replace": "patch",
    "run_shell_command": "exec",
    "shell": "exec",
    "google_web_search": "web_search",
    "web_fetch": "fetch",
    "write_todos": "plan",
    "enter_plan_mode": "plan",
    "exit_plan_mode": "plan",
    "tracker_create_task": "plan",
    "tracker_update_task": "plan",
    "tracker_get_task": "plan",
    "tracker_list_tasks": "plan",
    "invoke_agent": "agent",
    "activate_skill": "agent",
    "complete_task": "agent",
    "update_topic": "agent",
    "ask_user": "user_input",
    "take_snapshot": "snapshot",
}
_MUTATING_TOOLS = frozenset({"write_file", "replace"})
_MUTATING_SHELL = re.compile(
    r"(?:\brm\b|\bmv\b|\bcp\b|\bmkdir\b|\btouch\b|\bchmod\b|\bchown\b|"
    r"\bgit\s+(?:add|commit|push|reset|checkout|switch|merge|rebase)\b|>>?|sed\s+-i|\btee\b)",
    re.I,
)
_EXIT_CODE = re.compile(
    r"(?:Exit Code|Command exited with code):\s*(-?\d+)|\"exitCode\"\s*:\s*(-?\d+)"
)
_SESSION_CONTEXT_RE = re.compile(
    r"^\s*(<session_context>.*?</session_context>)\s*", re.S
)


def _unparsed(reason: str, data: bytes, size: int) -> dict[str, Any]:
    prefix = data[:_PREFIX_BYTES]
    return {
        "parse_error": reason,
        "byte_length": size,
        "raw": prefix.decode("utf-8", "replace"),
        "truncated": size > len(prefix),
    }


def _extract_part_text(content: Any) -> tuple[str, str, bool]:
    """Return ``(visible_text, thought_text, has_function_response_only)``."""
    if isinstance(content, str):
        return content, "", False
    if not isinstance(content, list):
        return "", "", False
    visible_parts: list[str] = []
    thought_parts: list[str] = []
    has_non_empty_text = False
    saw_function_response = False
    for part in content:
        if isinstance(part, str):
            if part:
                visible_parts.append(part)
                if part.strip():
                    has_non_empty_text = True
            continue
        if not isinstance(part, dict):
            continue
        if "functionResponse" in part:
            saw_function_response = True
            continue
        text = part.get("text")
        if isinstance(text, str):
            if part.get("thought") is True:
                if text.strip():
                    thought_parts.append(text)
            else:
                visible_parts.append(text)
                if text.strip():
                    has_non_empty_text = True
    visible = "\n".join(p for p in visible_parts if p).strip()
    thoughts = "\n\n".join(p.strip() for p in thought_parts if p.strip()).strip()
    synthetic_only = saw_function_response and not has_non_empty_text and not thoughts
    return visible, thoughts, synthetic_only


def _format_thoughts(raw_thoughts: Any) -> str:
    if not isinstance(raw_thoughts, list):
        return ""
    blocks: list[str] = []
    for item in raw_thoughts:
        if isinstance(item, str):
            text = item.strip()
            if text:
                blocks.append(text)
            continue
        if not isinstance(item, dict):
            continue
        subject = str(item.get("subject") or "").strip()
        description = str(item.get("description") or "").strip()
        if subject and description:
            blocks.append(f"**{subject}**\n{description}")
        elif subject:
            blocks.append(f"**{subject}**")
        elif description:
            blocks.append(description)
    return "\n\n".join(blocks)


def _tool_kind(name: str) -> str:
    normalized = name.strip().lower()
    if normalized in _TOOL_KINDS:
        return _TOOL_KINDS[normalized]
    if normalized.startswith("mcp_") or "__" in normalized:
        return "mcp"
    return normalized[:64] or "tool"


def _command_text(name: str, args: Any) -> str | None:
    if not isinstance(args, dict):
        return None
    if name in {"run_shell_command", "shell"}:
        for key in ("command", "cmd", "commandLine"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _is_mutating(name: str, command_text: str | None) -> bool:
    if name in _MUTATING_TOOLS:
        return True
    if name in {"run_shell_command", "shell"} and command_text:
        return bool(_MUTATING_SHELL.search(command_text))
    return False


def _arguments_json(args: Any) -> dict[str, Any] | None:
    if args is None:
        return None
    if isinstance(args, dict):
        return args or None
    if isinstance(args, list):
        return {"items": args} if args else None
    if isinstance(args, str):
        return {"raw": args} if args else None
    return {"value": args}


def _tool_output(call: dict[str, Any]) -> tuple[str | None, int | None]:
    """Extract readable output text and optional exit code from a Gemini toolCall."""
    exit_code: int | None = None
    chunks: list[str] = []

    result = call.get("result")
    if isinstance(result, str) and result.strip():
        chunks.append(result)
    elif isinstance(result, dict):
        if result.get("exitCode") is not None:
            exit_code = as_int(result["exitCode"], non_negative=False)
        chunks.append(json.dumps(result, ensure_ascii=False, sort_keys=True))
    elif isinstance(result, list):
        for item in result:
            if not isinstance(item, dict):
                continue
            fn_resp = item.get("functionResponse")
            if isinstance(fn_resp, dict):
                resp = fn_resp.get("response")
                if isinstance(resp, dict):
                    if resp.get("exitCode") is not None and exit_code is None:
                        exit_code = as_int(resp["exitCode"], non_negative=False)
                    for key in ("output", "error", "result", "content"):
                        val = resp.get(key)
                        if isinstance(val, str) and val.strip():
                            chunks.append(val)
                        elif val is not None and key != "output":
                            chunks.append(json.dumps(val, ensure_ascii=False))
                    if not chunks and resp:
                        chunks.append(json.dumps(resp, ensure_ascii=False, sort_keys=True))
                elif isinstance(resp, str) and resp.strip():
                    chunks.append(resp)
            elif "inlineData" in item:
                inline = item.get("inlineData")
                mime = (
                    str(inline.get("mimeType") or "binary")
                    if isinstance(inline, dict)
                    else "binary"
                )
                chunks.append(f"[inlineData: {mime}]")

    display = call.get("resultDisplay")
    if not chunks and display is not None:
        if isinstance(display, str):
            if display.strip():
                chunks.append(display)
        elif isinstance(display, dict):
            file_diff = display.get("fileDiff")
            if isinstance(file_diff, str) and file_diff.strip():
                chunks.append(file_diff)
            else:
                chunks.append(json.dumps(display, ensure_ascii=False, sort_keys=True))
        else:
            chunks.append(str(display))

    text = "\n\n".join(part for part in chunks if part)
    if exit_code is None and text:
        match = _EXIT_CODE.search(text)
        if match:
            raw_code = match.group(1) or match.group(2)
            if raw_code is not None:
                exit_code = as_int(raw_code, non_negative=False)
    return (text or None), exit_code


def _tool_status(
    raw_status: Any, *, exit_code: int | None, ongoing: bool
) -> str:
    status = str(raw_status or "").strip().lower()
    if status == "success":
        return "failed" if exit_code not in (None, 0) else "completed"
    if status == "error":
        return "failed"
    if status == "cancelled":
        return "cancelled"
    if status in {"validating", "scheduled", "executing", "awaiting_approval"}:
        return "running" if ongoing else "interrupted"
    if exit_code not in (None, 0):
        return "failed"
    return "completed" if status in {"done", "ok"} else (status[:32] or "pending")


@dataclass(frozen=True, slots=True)
class GeminiRecord:
    line_number: int
    byte_offset: int
    byte_length: int
    raw_json: dict[str, Any]
    raw_hash: str
    parsed: bool
    record_kind: str
    event_timestamp: datetime | None

    @property
    def known(self) -> bool:
        return self.parsed and self.record_kind != "unknown"


def _classify_record(decoded: dict[str, Any]) -> tuple[str, datetime | None]:
    if isinstance(decoded.get("$rewindTo"), str):
        return "rewind", None
    if isinstance(decoded.get("$patch"), dict):
        return "patch", None
    if isinstance(decoded.get("id"), str) and "$patch" not in decoded:
        msg_type = str(decoded.get("type") or "unknown")
        ts = parse_ts(decoded.get("timestamp"))
        if msg_type in _KNOWN_MESSAGE_TYPES:
            return msg_type, ts
        return "unknown", ts
    if isinstance(decoded.get("$set"), dict):
        set_obj = decoded["$set"]
        return "meta_set", parse_ts(set_obj.get("lastUpdated"))
    if isinstance(decoded.get("sessionId"), str) and (
        "projectHash" in decoded or "startTime" in decoded or "messages" in decoded
    ):
        return "session_meta", parse_ts(
            decoded.get("startTime") or decoded.get("lastUpdated")
        )
    return "unknown", parse_ts(decoded.get("timestamp") or decoded.get("lastUpdated"))


def iter_records(
    path: Path, *, offset: int = 0, line_offset: int = 0
) -> Iterator[GeminiRecord]:
    """Yield Gemini CLI JSONL records from ``offset`` with bounded memory."""
    line_number = line_offset
    for item in iter_raw_jsonl_lines(
        path,
        offset=offset,
        max_line_bytes=MAX_LINE_BYTES,
        prefix_bytes=_PREFIX_BYTES,
    ):
        if item.oversized_hash is not None:
            if not item.terminated:
                return
            line_number += 1
            yield GeminiRecord(
                line_number=line_number,
                byte_offset=item.byte_offset,
                byte_length=item.byte_length,
                raw_json=pg_safe(_unparsed("line_too_large", item.prefix, item.byte_length)),
                raw_hash=item.oversized_hash,
                parsed=False,
                record_kind="parse_error",
                event_timestamp=None,
            )
            continue
        assert item.raw is not None
        body = item.raw.rstrip(b"\r\n")
        if not body.strip():
            continue
        try:
            decoded = json.loads(body)
        except (UnicodeDecodeError, ValueError):
            decoded = None
        if not item.terminated and not isinstance(decoded, dict):
            return
        line_number += 1
        raw_hash = hashlib.sha256(body).hexdigest()
        if isinstance(decoded, dict):
            safe = pg_safe(decoded)
            kind, ts = _classify_record(safe)
            yield GeminiRecord(
                line_number=line_number,
                byte_offset=item.byte_offset,
                byte_length=item.byte_length,
                raw_json=safe,
                raw_hash=raw_hash,
                parsed=True,
                record_kind=kind,
                event_timestamp=ts,
            )
        else:
            reason = "invalid_json" if decoded is None else "not_an_object"
            yield GeminiRecord(
                line_number=line_number,
                byte_offset=item.byte_offset,
                byte_length=item.byte_length,
                raw_json=pg_safe(_unparsed(reason, body, len(body))),
                raw_hash=raw_hash,
                parsed=False,
                record_kind="parse_error",
                event_timestamp=None,
            )


class GeminiProjectIndex:
    """Read-only resolver for Gemini CLI's ``projects.json`` and ``.project_root``."""

    def __init__(self, gemini_home: Path) -> None:
        self.gemini_home = gemini_home
        self.projects_file = gemini_home / "projects.json"
        self._projects_sig: tuple[int, int] | None = None
        self._by_slug: dict[str, list[str]] = {}
        self._by_hash: dict[str, str] = {}
        self._root_cache: dict[Path, tuple[tuple[int, int] | None, str | None]] = {}

    def _projects_stat(self) -> tuple[int, int] | None:
        try:
            st = os.stat(self.projects_file)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _load_projects(self) -> None:
        sig = self._projects_stat()
        if sig == self._projects_sig:
            return
        by_slug: dict[str, list[str]] = {}
        by_hash: dict[str, str] = {}
        if sig is not None:
            try:
                raw = json.loads(self.projects_file.read_text("utf-8"))
                mapping = raw.get("projects") if isinstance(raw, dict) else None
                if isinstance(mapping, dict):
                    for raw_path, slug in mapping.items():
                        if not isinstance(raw_path, str) or not isinstance(slug, str):
                            continue
                        clean_path = raw_path.strip()
                        clean_slug = slug.strip()
                        if not clean_path or not clean_slug:
                            continue
                        by_slug.setdefault(clean_slug, []).append(clean_path)
                        digest = hashlib.sha256(clean_path.encode("utf-8")).hexdigest()
                        by_hash[digest] = clean_path
            except (OSError, UnicodeDecodeError, ValueError):
                pass
        self._projects_sig = sig
        self._by_slug = by_slug
        self._by_hash = by_hash

    @staticmethod
    def _root_stat(root_file: Path) -> tuple[int, int] | None:
        try:
            st = os.stat(root_file)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _read_project_root(self, slug_dir: Path) -> tuple[tuple[int, int] | None, str | None]:
        root_file = slug_dir / ".project_root"
        sig = self._root_stat(root_file)
        cached = self._root_cache.get(slug_dir)
        if cached is not None and cached[0] == sig:
            return cached
        value: str | None = None
        if sig is not None:
            try:
                text = root_file.read_text("utf-8", errors="replace").strip()
                if text:
                    value = text.splitlines()[0].strip()
            except OSError:
                value = None
        entry = (sig, value)
        self._root_cache[slug_dir] = entry
        return entry

    def digest(self, slug_dir: Path) -> str:
        """Cheap stat-only fingerprint of project metadata files."""
        proj_sig = self._projects_stat()
        root_sig = self._root_stat(slug_dir / ".project_root")
        payload = json.dumps([proj_sig, root_sig], separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def resolve_workspace(
        self,
        slug_dir: Path,
        *,
        project_hash: str | None = None,
        directories: list[str] | None = None,
    ) -> str | None:
        _, dot_root = self._read_project_root(slug_dir)
        self._load_projects()
        if project_hash and project_hash in self._by_hash:
            if dot_root:
                dot_hash = hashlib.sha256(dot_root.encode("utf-8")).hexdigest()
                if dot_hash == project_hash:
                    return dot_root
            return self._by_hash[project_hash]
        if dot_root:
            return dot_root
        candidates = self._by_slug.get(slug_dir.name)
        if candidates:
            return candidates[0]
        if directories:
            for directory in directories:
                if isinstance(directory, str) and directory.strip():
                    return directory.strip()
        return None


def _apply_single_patch(
    messages_map: dict[str, tuple[int, dict[str, Any]]], patch: dict[str, Any]
) -> None:
    msg_id = patch.get("id")
    if not isinstance(msg_id, str) or msg_id not in messages_map:
        return
    event_id, existing = messages_map[msg_id]
    updated = copy.deepcopy(existing)
    if "content" in patch and patch["content"] is not None:
        updated["content"] = patch["content"]
    patch_calls = patch.get("toolCalls")
    existing_calls = updated.get("toolCalls")
    if (
        isinstance(patch_calls, list)
        and updated.get("type") == "gemini"
        and isinstance(existing_calls, list)
    ):
        by_call_id = {
            tc.get("id"): tc
            for tc in existing_calls
            if isinstance(tc, dict) and isinstance(tc.get("id"), str)
        }
        for tc_patch in patch_calls:
            if not isinstance(tc_patch, dict):
                continue
            target = by_call_id.get(tc_patch.get("id"))
            if target is not None and "result" in tc_patch:
                target["result"] = tc_patch["result"]
    messages_map[msg_id] = (event_id, updated)


def _apply_replay_event(
    *,
    event_id: int,
    record_kind: str,
    raw: dict[str, Any],
    meta: dict[str, Any],
    messages_map: dict[str, tuple[int, dict[str, Any]]],
) -> None:
    if record_kind == "session_meta":
        for key in ("sessionId", "projectHash", "startTime", "lastUpdated", "kind", "summary"):
            if key in raw and raw[key] is not None:
                meta[key] = raw[key]
        if isinstance(raw.get("directories"), list):
            meta["directories"] = [
                str(d) for d in raw["directories"] if isinstance(d, str) and d.strip()
            ]
        if isinstance(raw.get("messages"), list):
            messages_map.clear()
            for msg in raw["messages"]:
                if isinstance(msg, dict) and isinstance(msg.get("id"), str):
                    messages_map[msg["id"]] = (event_id, msg)
        return

    if record_kind == "meta_set":
        set_obj = raw.get("$set")
        if not isinstance(set_obj, dict):
            return
        for key in ("sessionId", "projectHash", "startTime", "lastUpdated", "kind", "summary"):
            if key in set_obj and set_obj[key] is not None:
                meta[key] = set_obj[key]
        if isinstance(set_obj.get("directories"), list):
            meta["directories"] = [
                str(d)
                for d in set_obj["directories"]
                if isinstance(d, str) and d.strip()
            ]
        if isinstance(set_obj.get("messages"), list):
            messages_map.clear()
            for msg in set_obj["messages"]:
                if isinstance(msg, dict) and isinstance(msg.get("id"), str):
                    messages_map[msg["id"]] = (event_id, msg)
        return

    if record_kind == "rewind":
        rewind_id = raw.get("$rewindTo")
        if not isinstance(rewind_id, str):
            return
        if rewind_id not in messages_map:
            messages_map.clear()
            return
        drop = False
        to_remove: list[str] = []
        for mid in messages_map:
            if mid == rewind_id:
                drop = True
            if drop:
                to_remove.append(mid)
        for mid in to_remove:
            del messages_map[mid]
        return

    if record_kind == "patch":
        patch_obj = raw.get("$patch")
        if not isinstance(patch_obj, dict):
            return
        if isinstance(patch_obj.get("id"), str):
            _apply_single_patch(messages_map, patch_obj)
        updates = patch_obj.get("updates")
        if isinstance(updates, list):
            for upd in updates:
                if isinstance(upd, dict) and isinstance(upd.get("id"), str):
                    _apply_single_patch(messages_map, upd)
        remove_ids = patch_obj.get("removeIds")
        if isinstance(remove_ids, list):
            for rem_id in remove_ids:
                if isinstance(rem_id, str):
                    messages_map.pop(rem_id, None)
        order_ids = patch_obj.get("orderIds")
        if isinstance(order_ids, list):
            ordered: list[tuple[str, tuple[int, dict[str, Any]]]] = []
            seen_order: set[str] = set()
            for oid in order_ids:
                if isinstance(oid, str) and oid in messages_map and oid not in seen_order:
                    seen_order.add(oid)
                    ordered.append((oid, messages_map[oid]))
            prefix = [
                (mid, entry)
                for mid, entry in messages_map.items()
                if mid not in seen_order
            ]
            messages_map.clear()
            for mid, entry in prefix:
                messages_map[mid] = entry
            for mid, entry in ordered:
                messages_map[mid] = entry
        return

    if isinstance(raw.get("id"), str) and "$patch" not in raw:
        messages_map[raw["id"]] = (event_id, raw)


class GeminiCliSource:
    """Discover and project Gemini CLI conversations below ``<gemini_home>/tmp``."""

    kind = "gemini_jsonl"
    agent = "gemini"
    RELIST_INTERVAL = 600.0
    RACY_WINDOW = 2.0

    def __init__(self, root: str | Path, *, parser_version: str = "1") -> None:
        #: Gemini CLI home directory, e.g. ``~/.gemini``
        self.root = Path(root).expanduser()
        self.parser_version = parser_version
        self.projects = GeminiProjectIndex(self.root)
        self._slugs: list[str] | None = None
        self._slugs_mtime: int | None = None
        self._slugs_racy = False
        self._slugs_listed_at = 0.0
        #: slug name -> (chats_mtime_ns, racy, cached SourceDescriptors)
        self._chats_cache: dict[str, tuple[int, bool, list[SourceDescriptor]]] = {}

    @property
    def tmp_root(self) -> Path:
        return self.root / "tmp"

    @staticmethod
    def identity(path: Path) -> str:
        canonical = str(cached_resolve(path))
        return "gemini:" + hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def layout_for(path: Path) -> tuple[Path, str | None, str | None]:
        """Return ``(slug_dir, parent_session_id, fallback_thread_id)``."""
        if path.parent.name == "chats" and len(path.parents) >= 2:
            return path.parents[1], None, path.stem
        if len(path.parents) >= 3 and path.parents[1].name == "chats":
            return path.parents[2], path.parent.name, path.stem
        return path.parent, None, path.stem

    def _scan_chats(self, slug_dir: Path, *, verify: bool, now: float) -> list[SourceDescriptor]:
        chats = slug_dir / "chats"
        try:
            st = os.stat(chats)
        except OSError:
            self._chats_cache.pop(slug_dir.name, None)
            return []
        if not verify:
            cached = self._chats_cache.get(slug_dir.name)
            if (
                cached is not None
                and cached[0] == st.st_mtime_ns
                and not cached[1]
            ):
                return cached[2]

        jsonl_stems: set[str] = set()
        json_candidates: list[Path] = []
        found: list[Path] = []
        sub_dirs: list[Path] = []
        try:
            with os.scandir(chats) as entries:
                for entry in entries:
                    if entry.name.startswith("."):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        sub_dirs.append(Path(entry.path))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    p = Path(entry.path)
                    if entry.name.endswith(".jsonl"):
                        jsonl_stems.add(p.stem)
                        found.append(p)
                    elif entry.name.endswith(".json"):
                        json_candidates.append(p)
        except OSError:
            return []

        for sub_dir in sorted(sub_dirs):
            try:
                with os.scandir(sub_dir) as sub_entries:
                    for sub_entry in sub_entries:
                        if (
                            not sub_entry.name.startswith(".")
                            and sub_entry.name.endswith(".jsonl")
                            and sub_entry.is_file(follow_symlinks=False)
                        ):
                            found.append(Path(sub_entry.path))
            except OSError:
                continue

        for legacy_path in json_candidates:
            if legacy_path.stem not in jsonl_stems:
                found.append(legacy_path)

        descriptors = [
            SourceDescriptor(
                kind=self.kind,
                identity=self.identity(path),
                path=path,
                format="jsonl" if path.suffix == ".jsonl" else "json",
                agent=self.agent,
                thread_id=path.stem,
            )
            for path in sorted(found)
        ]
        racy = abs(now - st.st_mtime_ns / 1e9) < self.RACY_WINDOW or bool(sub_dirs)
        self._chats_cache[slug_dir.name] = (st.st_mtime_ns, racy, descriptors)
        return descriptors

    def discover(self) -> list[SourceDescriptor]:
        """List Gemini CLI chats below ``<gemini_home>/tmp/*/chats``."""
        tmp = self.tmp_root
        now = time.time()
        try:
            st = os.stat(tmp)
        except OSError:
            self._slugs = None
            self._chats_cache.clear()
            return []
        if (
            self._slugs is None
            or st.st_mtime_ns != self._slugs_mtime
            or self._slugs_racy
            or now - self._slugs_listed_at >= self.RELIST_INTERVAL
        ):
            try:
                with os.scandir(tmp) as entries:
                    names = sorted(
                        entry.name
                        for entry in entries
                        if not entry.name.startswith(".")
                        and entry.name != "background-processes"
                        and entry.is_dir(follow_symlinks=False)
                    )
            except OSError:
                return []
            self._slugs = names
            self._slugs_mtime = st.st_mtime_ns
            self._slugs_racy = abs(now - st.st_mtime_ns / 1e9) < self.RACY_WINDOW
            self._slugs_listed_at = now
            for gone in set(self._chats_cache) - set(names):
                del self._chats_cache[gone]

        verify = full_scan_requested()
        sources: list[SourceDescriptor] = []
        for slug in self._slugs:
            sources.extend(self._scan_chats(tmp / slug, verify=verify, now=now))
        return sources

    def watch_paths(self, source: SourceDescriptor) -> tuple[Path, ...]:
        """Files whose changes affect ``plan`` (session, projects.json, .project_root)."""
        slug_dir, _, _ = self.layout_for(source.path)
        return (source.path, self.projects.projects_file, slug_dir / ".project_root")

    def plan(self, source: SourceDescriptor, checkpoint: Any | None) -> SourcePlan:
        slug_dir, _, _ = self.layout_for(source.path)
        extra_fp = {"meta": self.projects.digest(slug_dir)}
        if source.format == "json":
            return plan_stat_rebuild(
                source, checkpoint, self.parser_version, extra_fp=extra_fp
            )
        return plan_append_jsonl(
            source,
            checkpoint,
            self.parser_version,
            extra_fp=extra_fp,
            check_path=True,
            check_prefix=True,
            rebuild_on_same_size=False,
            cached_path=True,
        )

    async def ingest(self, db: AsyncSession, plan: SourcePlan) -> AgentSession:
        source = plan.source
        slug_dir, parent_session_id, fallback_tid = self.layout_for(source.path)
        real_path = str(source.path.resolve())

        session = await db.scalar(
            select(AgentSession).where(AgentSession.source_identity == source.identity)
        )
        if session is None:
            session = AgentSession(
                id=stable_id("session", source.identity),
                thread_id=fallback_tid or source.path.stem,
                parent_session_id=parent_session_id,
                rollout_path=str(source.path),
                rollout_real_path=f"gemini:{real_path}",
                rollout_format=source.format,
                agent=self.agent,
                source_kind=self.kind,
                source_identity=source.identity,
                source_path=real_path,
                source_format=source.format,
                parser_version=self.parser_version,
                index_status="discovered",
                projected_event_index=-1,
                turn_count=0,
                input_tokens=0,
                cached_input_tokens=0,
                cache_creation_input_tokens=0,
                output_tokens=0,
                reasoning_output_tokens=0,
                total_tokens=0,
                last_indexed_offset=0,
                last_indexed_line=0,
            )
            db.add(session)
            await db.flush()
        else:
            session.agent = self.agent
            session.source_kind = self.kind
            session.source_format = source.format
            session.rollout_format = source.format
            session.rollout_path = str(source.path)
            session.rollout_real_path = f"gemini:{real_path}"
            session.source_path = real_path
            if parent_session_id:
                session.parent_session_id = parent_session_id

        if plan.action == PlanAction.UNCHANGED:
            return session

        if plan.action == PlanAction.REBUILD or session.parser_version != self.parser_version:
            await clear_session_rows(db, session.id)
            session.parser_version = self.parser_version
            session.last_indexed_offset = 0
            session.last_indexed_line = 0
            session.projected_event_index = -1
            session.turn_count = 0
            session.input_tokens = 0
            session.cached_input_tokens = 0
            session.cache_creation_input_tokens = 0
            session.output_tokens = 0
            session.reasoning_output_tokens = 0
            session.total_tokens = 0
            session.model = None
            session.cwd = None
            session.display_title = None
            session.ai_title = None
            session.thread_name = None
            session.start_time = None
            session.end_time = None
            session.last_activity_at = None
            session.thread_id = fallback_tid or source.path.stem
            session.parent_session_id = parent_session_id

        session.index_status = "indexing"
        session.last_error = None
        last_event_index = await db.scalar(
            select(func.max(AgentEvent.event_index)).where(
                AgentEvent.agent_session_id == session.id
            )
        )
        event_index = (last_event_index if last_event_index is not None else -1) + 1

        if source.format == "json":
            event_index = await self._ingest_legacy_json(db, session, source.path, event_index)
        else:
            await self._ingest_jsonl(db, session, source.path, event_index)

        stat = source.path.stat()
        session.file_size = stat.st_size
        session.file_mtime_ns = stat.st_mtime_ns
        await db.flush()
        await self._project(db, session, source, slug_dir)
        session.index_status = "indexed"
        await db.flush()
        return session

    async def _ingest_legacy_json(
        self, db: AsyncSession, session: AgentSession, path: Path, event_index: int
    ) -> int:
        stat = path.stat()
        if stat.st_size > _MAX_LEGACY_JSON_BYTES:
            digest = hashlib.sha256()
            prefix = b""
            with path.open("rb") as handle:
                while chunk := handle.read(64 * 1024):
                    if not prefix:
                        prefix = chunk[:_PREFIX_BYTES]
                    digest.update(chunk)
            db.add(
                AgentEvent(
                    agent_session_id=session.id,
                    event_index=event_index,
                    line_number=1,
                    byte_offset=0,
                    top_level_type="parse_error",
                    payload_type="parse_error",
                    normalized_type="parse_error",
                    raw_json=pg_safe(_unparsed("file_too_large", prefix, stat.st_size)),
                    raw_hash=digest.hexdigest(),
                    is_known=False,
                    parser_version=self.parser_version,
                )
            )
            session.last_indexed_offset = stat.st_size
            session.last_indexed_line = 1
            return event_index + 1

        raw_bytes = path.read_bytes()
        raw_hash = hashlib.sha256(raw_bytes).hexdigest()
        try:
            decoded = json.loads(raw_bytes)
        except (UnicodeDecodeError, ValueError):
            decoded = None
        if not isinstance(decoded, dict):
            reason = "invalid_json" if decoded is None else "not_an_object"
            db.add(
                AgentEvent(
                    agent_session_id=session.id,
                    event_index=event_index,
                    line_number=1,
                    byte_offset=0,
                    top_level_type="parse_error",
                    payload_type="parse_error",
                    normalized_type="parse_error",
                    raw_json=pg_safe(_unparsed(reason, raw_bytes, len(raw_bytes))),
                    raw_hash=raw_hash,
                    is_known=False,
                    parser_version=self.parser_version,
                )
            )
            session.last_indexed_offset = len(raw_bytes)
            session.last_indexed_line = 1
            return event_index + 1

        safe = pg_safe(decoded)
        messages = safe.get("messages") if isinstance(safe.get("messages"), list) else []
        header = {k: v for k, v in safe.items() if k != "messages"}
        header_ts = parse_ts(header.get("startTime") or header.get("lastUpdated"))
        db.add(
            AgentEvent(
                agent_session_id=session.id,
                event_index=event_index,
                line_number=1,
                byte_offset=0,
                event_timestamp=header_ts,
                top_level_type="session_meta",
                payload_type="session_meta",
                normalized_type="session_meta",
                raw_json=header,
                raw_hash=raw_hash,
                is_known=True,
                parser_version=self.parser_version,
            )
        )
        event_index += 1
        for msg_idx, msg in enumerate(messages, start=2):
            if not isinstance(msg, dict):
                continue
            msg_bytes = json.dumps(msg, ensure_ascii=False, sort_keys=True).encode("utf-8")
            kind, ts = _classify_record(msg)
            norm = _NORMALIZED_TYPES.get(kind, kind)
            db.add(
                AgentEvent(
                    agent_session_id=session.id,
                    event_index=event_index,
                    line_number=msg_idx,
                    byte_offset=0,
                    event_timestamp=ts,
                    top_level_type=kind,
                    payload_type=str(msg.get("type") or kind),
                    normalized_type=norm,
                    raw_json=msg,
                    raw_hash=hashlib.sha256(msg_bytes).hexdigest(),
                    is_known=kind != "unknown",
                    parser_version=self.parser_version,
                )
            )
            event_index += 1
        session.last_indexed_offset = len(raw_bytes)
        session.last_indexed_line = len(messages) + 1
        return event_index

    async def _ingest_jsonl(
        self, db: AsyncSession, session: AgentSession, path: Path, event_index: int
    ) -> None:
        offset = as_int(session.last_indexed_offset)
        line_offset = as_int(session.last_indexed_line)
        batch: list[AgentEvent] = []
        last_record: GeminiRecord | None = None
        for record in iter_records(path, offset=offset, line_offset=line_offset):
            raw = record.raw_json
            norm = _NORMALIZED_TYPES.get(record.record_kind, record.record_kind)
            payload_type = (
                str(raw.get("type") or record.record_kind)
                if record.parsed
                else "parse_error"
            )
            batch.append(
                AgentEvent(
                    agent_session_id=session.id,
                    event_index=event_index,
                    line_number=record.line_number,
                    byte_offset=record.byte_offset,
                    event_timestamp=record.event_timestamp,
                    top_level_type=record.record_kind,
                    payload_type=payload_type,
                    normalized_type=norm,
                    raw_json=raw,
                    raw_hash=record.raw_hash,
                    is_known=record.known,
                    parser_version=self.parser_version,
                )
            )
            event_index += 1
            last_record = record
            if len(batch) >= _EVENT_BATCH:
                db.add_all(batch)
                await db.flush()
                batch.clear()
        if batch:
            db.add_all(batch)
            await db.flush()
        if last_record is not None:
            session.last_indexed_offset = (
                last_record.byte_offset + last_record.byte_length
            )
            session.last_indexed_line = last_record.line_number

    async def _project(
        self,
        db: AsyncSession,
        session: AgentSession,
        source: SourceDescriptor,
        slug_dir: Path,
    ) -> None:
        maximum = await db.scalar(
            select(func.max(AgentEvent.event_index)).where(
                AgentEvent.agent_session_id == session.id
            )
        )
        if maximum is not None and maximum == session.projected_event_index:
            # Workspace registry files (.project_root / projects.json) can change
            # independently of the transcript stream; keep metadata synced.
            meta_events = await db.scalars(
                select(AgentEvent)
                .where(
                    AgentEvent.agent_session_id == session.id,
                    AgentEvent.top_level_type.in_(("session_meta", "meta_set")),
                )
                .order_by(AgentEvent.event_index)
            )
            project_hash: str | None = None
            directories: list[str] | None = None
            for ev in meta_events:
                payload = ev.raw_json.get("$set") if ev.top_level_type == "meta_set" else ev.raw_json
                if isinstance(payload, dict):
                    if isinstance(payload.get("projectHash"), str):
                        project_hash = payload["projectHash"]
                    if isinstance(payload.get("directories"), list):
                        directories = [
                            str(d) for d in payload["directories"] if isinstance(d, str)
                        ]
            cwd = self.projects.resolve_workspace(
                slug_dir, project_hash=project_hash, directories=directories
            )
            if cwd:
                session.cwd = cwd
            session.is_ongoing = self._is_ongoing(source.path, session.end_time)
            return

        await clear_session_rows(db, session.id, include_events=False)

        meta: dict[str, Any] = {}
        messages_map: dict[str, tuple[int, dict[str, Any]]] = {}
        events = await db.stream(
            select(
                AgentEvent.id,
                AgentEvent.event_index,
                AgentEvent.top_level_type,
                AgentEvent.raw_json,
                AgentEvent.event_timestamp,
            )
            .where(AgentEvent.agent_session_id == session.id)
            .order_by(AgentEvent.event_index)
            .execution_options(yield_per=200)
        )
        async for ev_id, _ev_idx, ev_kind, ev_raw, _ev_ts in events:
            raw = ev_raw if isinstance(ev_raw, dict) else {}
            _apply_replay_event(
                event_id=int(ev_id),
                record_kind=str(ev_kind or ""),
                raw=raw,
                meta=meta,
                messages_map=messages_map,
            )

        if isinstance(meta.get("sessionId"), str) and meta["sessionId"].strip():
            session.thread_id = meta["sessionId"].strip()

        cwd = self.projects.resolve_workspace(
            slug_dir,
            project_hash=meta.get("projectHash")
            if isinstance(meta.get("projectHash"), str)
            else None,
            directories=meta.get("directories")
            if isinstance(meta.get("directories"), list)
            else None,
        )
        if cwd:
            session.cwd = cwd

        summary = meta.get("summary")
        if isinstance(summary, str) and summary.strip():
            session.ai_title = " ".join(summary.split())[:255]

        session.start_time = parse_ts(meta.get("startTime"))
        end_time = parse_ts(meta.get("lastUpdated"))

        turns: list[AgentTurn] = []
        current_turn: AgentTurn | None = None
        seen_tools: dict[str, ToolCall] = {}
        ordered_tools: list[ToolCall] = []
        sequence = 0
        first_user: str | None = None
        last_model: str | None = None
        input_total = 0
        cached_total = 0
        output_total = 0
        reasoning_total = 0
        tokens_total = 0
        pending_error = False

        ongoing = self._is_ongoing(source.path, end_time)

        def finish_turn(ts: datetime | None, *, status: str = "completed") -> None:
            nonlocal current_turn
            if current_turn is None:
                return
            if ts is not None and (
                current_turn.completed_at is None or ts > current_turn.completed_at
            ):
                current_turn.completed_at = ts
            current_turn.status = status
            current_turn.outcome = status
            current_turn.duration_ms = duration_ms(
                current_turn.started_at, current_turn.completed_at
            )
            current_turn = None

        def open_turn(ts: datetime | None) -> AgentTurn:
            nonlocal current_turn, pending_error
            turn_index = len(turns)
            turn = AgentTurn(
                id=stable_id("turn", f"{session.id}:{turn_index}"),
                agent=self.agent,
                agent_session_id=session.id,
                turn_index=turn_index,
                turn_id=f"turn-{turn_index}",
                status="running",
                outcome="running",
                started_at=ts,
                input_tokens=0,
                cached_input_tokens=0,
                cache_creation_input_tokens=0,
                output_tokens=0,
                reasoning_output_tokens=0,
                total_tokens=0,
            )
            db.add(turn)
            turns.append(turn)
            current_turn = turn
            pending_error = False
            return turn

        def add_message(
            *,
            event_id: int,
            msg_key: str,
            role: str,
            channel: str,
            text: str,
            ts: datetime | None,
            system: bool = False,
        ) -> None:
            nonlocal sequence
            if not text:
                return
            db.add(
                CodexMessage(
                    id=stable_id("msg", f"{session.id}:{msg_key}:{sequence}"),
                    agent_session_id=session.id,
                    event_id=event_id,
                    turn_id=current_turn.id if current_turn else None,
                    role=role,
                    channel=channel,
                    text=text,
                    markdown_text=text,
                    copy_text=text,
                    sequence=sequence,
                    timestamp=ts,
                    is_visible=channel != "reasoning",
                    is_system_generated=system,
                )
            )
            sequence += 1

        for msg_id, (event_id, rec) in messages_map.items():
            msg_type = str(rec.get("type") or "unknown")
            ts = parse_ts(rec.get("timestamp"))
            if ts is not None:
                if session.start_time is None or ts < session.start_time:
                    session.start_time = ts
                if end_time is None or ts > end_time:
                    end_time = ts

            model = rec.get("model")
            if isinstance(model, str) and model.strip():
                last_model = model.strip()[:128]

            if msg_type == "user":
                visible, inline_thoughts, synthetic_only = _extract_part_text(
                    rec.get("content")
                )
                display_content = rec.get("displayContent")
                if not visible and isinstance(display_content, (str, list)):
                    disp_visible, _, _ = _extract_part_text(display_content)
                    if disp_visible:
                        visible = disp_visible
                        synthetic_only = False

                # Link functionResponse timestamps back onto matching tool calls.
                content_list = rec.get("content")
                if isinstance(content_list, list) and ts is not None:
                    for part in content_list:
                        fn_resp = part.get("functionResponse") if isinstance(part, dict) else None
                        if isinstance(fn_resp, dict):
                            call_id = fn_resp.get("id")
                            if isinstance(call_id, str) and call_id in seen_tools:
                                tool = seen_tools[call_id]
                                if tool.completed_at is None:
                                    tool.completed_at = ts

                if synthetic_only or not visible:
                    continue

                ctx_match = _SESSION_CONTEXT_RE.match(visible)
                if ctx_match:
                    add_message(
                        event_id=event_id,
                        msg_key=f"{msg_id}:ctx",
                        role="system",
                        channel="commentary",
                        text=ctx_match.group(1).strip(),
                        ts=ts,
                        system=True,
                    )
                    visible = visible[ctx_match.end() :].strip()
                    if not visible:
                        continue

                if current_turn is not None:
                    finish_turn(
                        ts, status="failed" if pending_error else "completed"
                    )
                open_turn(ts)
                if first_user is None:
                    first_user = " ".join(visible.split())[:255]
                add_message(
                    event_id=event_id,
                    msg_key=msg_id,
                    role="user",
                    channel="final",
                    text=visible,
                    ts=ts,
                )
                continue

            if msg_type == "gemini":
                if current_turn is None:
                    open_turn(ts)
                assert current_turn is not None
                pending_error = False

                tokens = rec.get("tokens")
                if isinstance(tokens, dict):
                    t_in = as_int(tokens.get("input"))
                    t_cached = as_int(tokens.get("cached"))
                    t_out = as_int(tokens.get("output"))
                    t_thoughts = as_int(tokens.get("thoughts"))
                    t_tool = as_int(tokens.get("tool"))
                    t_total = as_int(tokens.get("total")) or (
                        t_in + t_cached + t_out + t_thoughts + t_tool
                    )
                    input_total += t_in
                    cached_total += t_cached
                    output_total += t_out
                    reasoning_total += t_thoughts
                    tokens_total += t_total
                    current_turn.input_tokens = (current_turn.input_tokens or 0) + t_in
                    current_turn.cached_input_tokens = (
                        current_turn.cached_input_tokens or 0
                    ) + t_cached
                    current_turn.output_tokens = (
                        current_turn.output_tokens or 0
                    ) + t_out
                    current_turn.reasoning_output_tokens = (
                        current_turn.reasoning_output_tokens or 0
                    ) + t_thoughts
                    current_turn.total_tokens = (
                        current_turn.total_tokens or 0
                    ) + t_total

                visible, inline_thoughts, _ = _extract_part_text(rec.get("content"))
                thought_text = _format_thoughts(rec.get("thoughts"))
                if inline_thoughts:
                    thought_text = (
                        f"{thought_text}\n\n{inline_thoughts}".strip()
                        if thought_text
                        else inline_thoughts
                    )
                if thought_text:
                    add_message(
                        event_id=event_id,
                        msg_key=msg_id,
                        role="assistant",
                        channel="reasoning",
                        text=thought_text,
                        ts=ts,
                    )
                if visible:
                    add_message(
                        event_id=event_id,
                        msg_key=msg_id,
                        role="assistant",
                        channel="final",
                        text=visible,
                        ts=ts,
                    )

                tool_calls = rec.get("toolCalls")
                if isinstance(tool_calls, list):
                    for idx, tc in enumerate(tool_calls):
                        if not isinstance(tc, dict):
                            continue
                        call_id = str(tc.get("id") or f"{msg_id}:call:{idx}")[:128]
                        tool_name = str(tc.get("name") or tc.get("displayName") or "tool")[:128]
                        args = tc.get("args")
                        cmd_text = _command_text(tool_name, args)
                        output_text, exit_code = _tool_output(tc)
                        started_at = parse_ts(tc.get("timestamp")) or ts
                        status = _tool_status(
                            tc.get("status"), exit_code=exit_code, ongoing=ongoing
                        )
                        completed_at = (
                            ts if status in {"completed", "failed", "cancelled"} else None
                        )
                        existing_tool = seen_tools.get(call_id)
                        if existing_tool is None:
                            new_tool = ToolCall(
                                id=stable_id("tool", f"{session.id}:{call_id}"),
                                agent=self.agent,
                                agent_session_id=session.id,
                                event_id=event_id,
                                turn_id=current_turn.id,
                                call_id=call_id,
                                tool_name=tool_name,
                                tool_kind=_tool_kind(tool_name),
                                arguments_json=_arguments_json(args),
                                output_text=output_text,
                                command_text=cmd_text,
                                status=status,
                                exit_code=exit_code,
                                is_mutating=_is_mutating(tool_name, cmd_text),
                                started_at=started_at,
                                completed_at=completed_at,
                            )
                            db.add(new_tool)
                            seen_tools[call_id] = new_tool
                            ordered_tools.append(new_tool)
                        else:
                            existing_tool.event_id = event_id
                            existing_tool.turn_id = current_turn.id
                            existing_tool.tool_name = tool_name
                            existing_tool.tool_kind = _tool_kind(tool_name)
                            existing_tool.arguments_json = _arguments_json(args)
                            existing_tool.output_text = output_text
                            existing_tool.command_text = cmd_text
                            existing_tool.status = status
                            existing_tool.exit_code = exit_code
                            existing_tool.is_mutating = _is_mutating(tool_name, cmd_text)
                            existing_tool.completed_at = completed_at or existing_tool.completed_at

                if ts is not None:
                    current_turn.completed_at = ts
                continue

            if msg_type in {"info", "warning", "error"}:
                visible, _, _ = _extract_part_text(rec.get("content"))
                if msg_type == "error":
                    pending_error = True
                    if current_turn is not None:
                        current_turn.status = "failed"
                        current_turn.outcome = "failed"
                if visible:
                    prefix = f"[{msg_type}] " if msg_type != "info" else ""
                    add_message(
                        event_id=event_id,
                        msg_key=msg_id,
                        role="system",
                        channel="commentary",
                        text=f"{prefix}{visible}",
                        ts=ts,
                        system=True,
                    )

        if current_turn is not None:
            if not ongoing:
                finish_turn(
                    end_time, status="failed" if pending_error else "completed"
                )
            elif any(
                tc.status == "running"
                for tc in ordered_tools
                if tc.turn_id == current_turn.id
            ):
                current_turn.status = current_turn.outcome = "running"

        await db.flush()

        for turn in turns:
            if turn.started_at is None:
                continue
            db.add(
                ActivitySpan(
                    id=stable_id("span", f"turn:{turn.id}"),
                    agent=self.agent,
                    source_kind=self.kind,
                    source_record_id=f"turn:{turn.turn_index}",
                    agent_session_id=session.id,
                    thread_id=session.thread_id,
                    turn_id=turn.turn_id,
                    span_type="turn",
                    name=f"Turn {turn.turn_index + 1}",
                    status=turn.status,
                    started_at=turn.started_at,
                    completed_at=turn.completed_at,
                    duration_ms=turn.duration_ms,
                    confidence="derived",
                    metadata_json={"agent": self.agent},
                )
            )

        for tool in ordered_tools:
            if tool.started_at is None:
                continue
            db.add(
                ActivitySpan(
                    id=stable_id("span", f"tool:{tool.id}"),
                    agent=self.agent,
                    source_kind=self.kind,
                    source_record_id=f"tool:{tool.call_id}",
                    agent_session_id=session.id,
                    thread_id=session.thread_id,
                    turn_id=tool.turn_id,
                    span_type=tool.tool_kind,
                    name=tool.tool_name,
                    status=tool.status,
                    started_at=tool.started_at,
                    completed_at=tool.completed_at,
                    duration_ms=duration_ms(tool.started_at, tool.completed_at),
                    confidence="derived",
                    metadata_json={"agent": self.agent},
                )
            )

        session.model = last_model
        session.turn_count = len(turns)
        session.input_tokens = input_total
        session.cached_input_tokens = cached_total
        session.output_tokens = output_total
        session.reasoning_output_tokens = reasoning_total
        session.total_tokens = tokens_total
        session.end_time = end_time or session.start_time
        session.last_activity_at = end_time or session.start_time
        session.is_ongoing = ongoing
        if first_user:
            session.display_title = first_user
        elif session.ai_title:
            session.display_title = session.ai_title
        if session.ai_title and not session.thread_name:
            session.thread_name = session.ai_title
        elif not session.thread_name and session.parent_session_id:
            session.thread_name = session.thread_id
        session.projected_event_index = maximum if maximum is not None else -1

    @staticmethod
    def _is_ongoing(path: Path, last_ts: datetime | None) -> bool:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return False
        now = datetime.now(UTC)
        return (now.timestamp() - mtime) < ONGOING_SECONDS and (
            last_ts is not None
            and (now - last_ts).total_seconds() < ONGOING_SECONDS
        )


__all__ = [
    "GeminiCliSource",
    "GeminiProjectIndex",
    "GeminiRecord",
    "iter_records",
]
