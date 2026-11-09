"""Small, forward-compatible Codex JSONL normalizer.

The parser deliberately keeps the complete JSON object for every event. New
Codex event types are indexed as ``unknown`` rather than discarded.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

KNOWN = {
    "session_meta", "response_item", "event_msg", "turn_context", "compacted",
    "session_meta_root", "state_placeholder", "partial_view", "world_state",
    "message", "function_call", "function_call_output",
    "custom_tool_call", "custom_tool_call_output", "reasoning", "web_search_call",
    "structured_output", "token_count", "token_usage_record", "additional_tools",
}

#: ``event_msg`` payload types observed in real rollouts (Codex v0.146-0.149).
#: These are surfaced as their concrete payload type (and marked ``is_known``)
#: even though they are not top-level ``type`` values.
KNOWN_EVENT_MSG = {
    "user_message", "agent_message", "agent_reasoning", "token_count",
    "context_compacted", "task_started", "task_complete", "turn_aborted",
    "turn_started", "turn_completed", "turn_failed", "patch_apply_begin",
    "patch_apply_end", "web_search_begin", "web_search_end",
    "exec_command_begin", "exec_command_end", "thread_settings_applied",
    "thread_rolled_back", "error", "warning", "item_completed",
}


@dataclass(frozen=True)
class NormalizedEvent:
    top_level_type: str | None
    payload_type: str | None
    normalized_type: str
    is_known: bool
    raw_json: dict[str, Any]
    raw_hash: str
    timestamp: datetime | None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _strip_nul(obj: Any) -> Any:
    """Recursively escape NUL (``\\u0000``) in a parsed rollout event.

    Codex tool output occasionally contains literal NUL bytes (e.g. grepping
    binary files). PostgreSQL ``text``/``jsonb`` cannot store ``\\u0000``, so it
    is represented as the literal text ``\\u0000`` before reaching the
    ``raw_json`` column. The raw hash is still taken over the original line.
    """
    if isinstance(obj, str):
        return obj.replace("\x00", "\\u0000") if "\x00" in obj else obj
    if isinstance(obj, dict):
        return {_strip_nul(key): _strip_nul(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_strip_nul(item) for item in obj]
    return obj


def parse_json_line(line: str) -> NormalizedEvent:
    import json

    raw = json.loads(line)
    if not isinstance(raw, dict):
        raise ValueError("rollout event must be a JSON object")
    top = (
        "state_placeholder"
        if raw.get("record_type") == "state"
        else raw.get("type")
    )
    if top is None:
        if "payload" in raw:
            top = "session_meta"
        elif raw.get("id") is not None and raw.get("timestamp") is not None:
            top = "session_meta_root"
        elif all(raw.get(key) is not None for key in ("call_id", "arguments", "name")):
            top = "function_call"
        elif raw.get("call_id") is not None and raw.get("output") is not None:
            top = "function_call_output"
        elif raw.get("role") is not None and raw.get("content") is not None:
            top = "message"
        elif raw.get("encrypted_content") is not None:
            top = "reasoning"
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        payload = raw
    payload_type = payload.get("type") if isinstance(payload, dict) else None
    if top in {
        "message",
        "function_call",
        "function_call_output",
        "reasoning",
        "session_meta_root",
    }:
        payload_type = top
    view_mode = raw.get("view_mode")
    if isinstance(view_mode, str) and view_mode != "full":
        payload_type = "partial_view"
    candidate = str(payload_type or top or "unknown")
    normalized = candidate if candidate in KNOWN else "unknown"
    if top == "response_item" and payload_type in KNOWN:
        normalized = str(payload_type)
    if top == "event_msg" and payload_type in KNOWN_EVENT_MSG:
        normalized = str(payload_type)
    return NormalizedEvent(
        top_level_type=str(top) if top is not None else None,
        payload_type=str(payload_type) if payload_type is not None else None,
        normalized_type=normalized,
        is_known=normalized != "unknown",
        raw_json=_strip_nul(raw),
        raw_hash=hashlib.sha256(line.encode("utf-8")).hexdigest(),
        timestamp=_timestamp(raw.get("timestamp")),
    )
