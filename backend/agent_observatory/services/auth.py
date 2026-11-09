"""Authentication primitives: password hashing, principal, session tokens.

Zero third-party deps: scrypt via stdlib `hashlib`, constant-time compare via
`hmac`. Mirrors resilient-codex-tasks-dev's `services/auth.py` so the security
posture (argon-class work factor, fixed-time comparison, never storing raw
tokens) is identical. Tokens are stored as `sha256(token)`; the browser holds
the raw token in an HttpOnly cookie.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass

ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_VIEWER = "viewer"
KNOWN_ROLES: frozenset[str] = frozenset({ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER})


@dataclass(frozen=True)
class Principal:
    user_id: str
    username: str
    roles: frozenset[str]
    bootstrap: bool = False

    @property
    def is_admin(self) -> bool:
        return ROLE_ADMIN in self.roles

    @property
    def can_operate(self) -> bool:
        return bool(self.roles & {ROLE_ADMIN, ROLE_OPERATOR})

    def has_role(self, *names: str) -> bool:
        return bool(self.roles & set(names))


# --- passwords -------------------------------------------------------------


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("Password must contain at least 10 characters")
    salt = os.urandom(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    )
    return "scrypt$16384$8$1$" + "$".join(
        (
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(derived).decode("ascii"),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_text, digest_text = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_text)
        expected = base64.urlsafe_b64decode(digest_text)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# --- tokens ----------------------------------------------------------------


def token_digest(token: str) -> bytes:
    """sha256(token) — what we store; raw token lives only in the cookie."""
    return hashlib.sha256(token.encode("utf-8")).digest()


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def new_user_id() -> str:
    return secrets.token_hex(8)


def new_session_id() -> str:
    return secrets.token_hex(8)
