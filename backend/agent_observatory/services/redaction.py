"""Best-effort display redaction for secrets persisted in rollout command text."""
from __future__ import annotations

import re
from typing import Any

_OPENAI_KEY = re.compile(r"sk-[A-Za-z0-9]{20,}")
_AWS_KEY = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_BEARER = re.compile(r"(?i:\bBearer)[ \t]+[A-Za-z0-9._~+/-]{16,}=*")
_URL_USERINFO = re.compile(
    r"(?i)\b((?:postgres(?:ql)?|redis|https?)://[^:/\s]+:)[^@\s]+(@)"
)
_COOKIE = re.compile(r"(?i)\b(cookie|set-cookie)(\s*[:=]\s*)[^\r\n]+")
_ASSIGNMENT = re.compile(
    r"""(?i)\b(api[_-]?key|token|secret|password)\b(\s*[:=]\s*)(["']?)[^\s"']{8,}"""
)


def redact_secrets(value: str | None) -> str | None:
    if value is None or not value:
        return value
    redacted = _BEARER.sub("Bearer [REDACTED_SECRET]", value)
    redacted = _OPENAI_KEY.sub("[REDACTED_SECRET]", redacted)
    redacted = _AWS_KEY.sub("[REDACTED_SECRET]", redacted)
    redacted = _URL_USERINFO.sub(r"\1[REDACTED_SECRET]\2", redacted)
    redacted = _COOKIE.sub(r"\1\2[REDACTED_SECRET]", redacted)
    return _ASSIGNMENT.sub(r"\1\2\3[REDACTED_SECRET]", redacted)


def redact_value(value: Any) -> Any:
    """Recursively redact display/export copies while preserving raw DB rows."""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {str(key): redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item) for item in value]
    return value


__all__ = ["redact_secrets", "redact_value"]
