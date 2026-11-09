"""Workspace linking and title backfill for indexed sources."""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, Final

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent_observatory.config import settings
from agent_observatory.db.models import AgentSession, CodexMessage, Workspace

#: Characters ``str.strip()`` removes that matter for titles.  PostgreSQL's
#: one-argument ``btrim`` only trims spaces, so the character set is explicit.
_WHITESPACE: Final[str] = " \t\n\r\x0b\x0c"


def has_visible_text(column: Any) -> Any:
    """SQL predicate matching text with at least one visible character.

    Callers that *consume* a message (title backfill) and callers that *select*
    candidates for it must use the same predicate, otherwise a row that can
    never be written stays in the candidate window forever.
    """
    return and_(column.is_not(None), func.btrim(column, _WHITESPACE) != "")


class SessionLinker:
    """Associate indexed sessions with normalized workspaces.

    ``cwd`` is an execution detail emitted by the source agent.  It is not
    necessarily a durable project checkout (for example, Codex executors often
    use ``/tmp/tmp*`` sandboxes).  ``classify_workspace`` provides a small,
    deterministic semantic layer so callers can distinguish persistent
    projects from ephemeral, test, system, and deleted paths while retaining
    the original cwd on the session.
    """

    _SYSTEM_PATHS: Final[frozenset[str]] = frozenset(
        {
            "/",
            "/bin",
            "/boot",
            "/dev",
            "/etc",
            "/home",
            "/lib",
            "/lib64",
            "/opt",
            "/proc",
            "/root",
            "/run",
            "/sbin",
            "/srv",
            "/sys",
            "/tmp",
            "/usr",
            "/var",
        }
    )

    @staticmethod
    def normalize_cwd(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        raw = value.strip()
        if not raw or "\x00" in raw:
            return None
        try:
            candidate = Path(raw).expanduser().resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return None
        return str(candidate) if candidate.is_absolute() else None

    #: Classification probes the filesystem (``exists`` + ``.git`` walks).
    #: Those answers rarely change, and the indexer used to pay for them once
    #: per session on every poll, so results are memoized with a short TTL.
    _CLASSIFY_TTL_SECONDS: Final[float] = 300.0
    _CLASSIFY_CACHE_MAX: Final[int] = 4096
    _classify_cache: Final[dict[Any, tuple[float, tuple[str, str]]]] = {}

    @classmethod
    def clear_classification_cache(cls) -> None:
        """Drop memoized classification results (used by tests and callers)."""
        cls._classify_cache.clear()

    @classmethod
    def classify_workspace(cls, normalized_path: str) -> tuple[str, str]:
        """Return memoized ``(workspace_kind, confidence)`` for a path.

        The heuristic rules are documented on
        :meth:`_classify_workspace_uncached`; this wrapper only adds caching
        because the probe touches the filesystem.  The cache key includes the
        configured allowed roots so policy changes are visible immediately.
        """
        try:
            configured_roots = tuple(settings().allowed_workspaces)
        except Exception:  # noqa: BLE001 - classification must never fail hard
            configured_roots = ()
        key = (normalized_path, configured_roots)
        now = time.monotonic()
        cached = cls._classify_cache.get(key)
        if cached is not None and now - cached[0] < cls._CLASSIFY_TTL_SECONDS:
            return cached[1]
        result = cls._classify_workspace_uncached(normalized_path)
        if len(cls._classify_cache) >= cls._CLASSIFY_CACHE_MAX:
            cls._classify_cache.clear()
        cls._classify_cache[key] = (now, result)
        return result

    @classmethod
    def _classify_workspace_uncached(cls, normalized_path: str) -> tuple[str, str]:
        """Classify one path by probing the filesystem (see public wrapper).

        Classification intentionally uses conservative, explainable heuristics:

        * known host/system roots are ``system_directory``;
        * pytest and common temporary-fixture names are ``test_fixture``;
        * Codex/RCW style ``/tmp/tmp*`` sandboxes are ``ephemeral_task``;
        * an existing path inside a git checkout (or configured allowed root)
          is ``persistent_project``;
        * missing paths with no stronger signal are ``deleted_path``;
        * everything else is ``unknown``.

        The order matters: a deleted temporary sandbox remains useful history
        and is therefore classified by its explicit ``/tmp`` semantic rather
        than only as a missing path.
        """

        path = Path(normalized_path)
        text = str(path)
        name = path.name.lower()

        # Avoid presenting host roots as user projects.  Only exact roots are
        # treated as system directories; children such as /tmp/my-project are
        # evaluated by the temporary/project rules below.
        if text in cls._SYSTEM_PATHS:
            return "system_directory", "high"

        # Existing checkout (or a subdirectory thereof) is a durable project.
        # Checking parents handles sessions launched from nested directories.
        try:
            if path.exists() and any(
                (candidate / ".git").exists() for candidate in (path, *path.parents)
            ):
                return "persistent_project", "high"
        except OSError:
            # A disappearing/unreadable path is still retained as unknown
            # rather than failing the indexing transaction.
            return "unknown", "low"

        # Pytest and other fixture directories are short-lived by design.  This
        # check follows git detection so a real checkout created under a test
        # harness is still recognized as a persistent project.
        if path.parent == Path("/tmp") and (
            name.startswith("pytest-")
            or name.startswith(".pytest-")
            or name.startswith("tmppytest-")
        ):
            return "test_fixture", "high"
        if any(part.startswith("pytest-of-") for part in path.parts):
            return "test_fixture", "high"

        # Codex executors and task runners conventionally allocate /tmp/tmp*
        # (plus rcw/task variants).  Keep this classification even after the
        # directory has been cleaned up so historical sessions remain grouped.
        if path.parent == Path("/tmp") and (
            name.startswith("tmp")
            or name.startswith("rcw-")
            or name.startswith("task-")
        ):
            return "ephemeral_task", "high"
        if any(part.startswith("rcw-") for part in path.parts):
            return "ephemeral_task", "high"

        # A path that no longer exists cannot be verified as a project.
        if not path.exists():
            return "deleted_path", "low"

        # Existing explicitly configured roots are considered projects even if
        # they do not use git (for example, a monorepo export or generated
        # workspace). Missing paths remain ``deleted_path`` above.
        try:
            configured = [cls.normalize_cwd(item) for item in settings().allowed_workspaces]
            roots = [item for item in configured if item is not None]
            if any(text == root or Path(root) in path.parents for root in roots):
                return "persistent_project", "medium"
        except (OSError, RuntimeError, ValueError):
            pass

        return "unknown", "low"

    async def backfill_title(self, db: AsyncSession, session: AgentSession) -> None:
        if session.display_title:
            return
        first_user = await db.scalar(
            select(CodexMessage.text)
            .where(
                CodexMessage.agent_session_id == session.id,
                CodexMessage.role == "user",
                has_visible_text(CodexMessage.text),
            )
            .order_by(CodexMessage.sequence)
            .limit(1)
        )
        if isinstance(first_user, str) and first_user.strip():
            session.display_title = " ".join(first_user.split())[:255]
            session.ai_title = session.ai_title or session.display_title

    async def link_workspace(self, db: AsyncSession, session: AgentSession) -> None:
        normalized = self.normalize_cwd(session.cwd)
        if normalized is None:
            if session.cwd is not None:
                session.cwd = None
            return
        workspace = await db.scalar(
            select(Workspace).where(Workspace.normalized_path == normalized)
        )
        if workspace is None:
            roots = [self.normalize_cwd(item) for item in settings().allowed_workspaces]
            allowed = [item for item in roots if item is not None]
            is_allowed = not allowed or any(
                normalized == root or Path(root) in Path(normalized).parents
                for root in allowed
            )
            workspace_id = f"workspace-{hashlib.sha256(normalized.encode()).hexdigest()[:48]}"
            workspace_kind, workspace_confidence = self.classify_workspace(normalized)
            workspace_values = {
                "id": workspace_id,
                "path": normalized,
                "normalized_path": normalized,
                "display_name": Path(normalized).name or normalized,
                "is_allowed": is_allowed,
            }
            # ``Workspace`` gains these columns in the workspace semantics
            # migration.  Keeping this guard makes old databases/import tests
            # continue to work while allowing the indexer to populate them as
            # soon as the model is upgraded.
            if "workspace_kind" in Workspace.__table__.columns:
                workspace_values["workspace_kind"] = workspace_kind
            if "workspace_confidence" in Workspace.__table__.columns:
                workspace_values["workspace_confidence"] = workspace_confidence
            await db.execute(
                pg_insert(Workspace)
                .values(**workspace_values)
                .on_conflict_do_nothing(index_elements=[Workspace.normalized_path])
            )
            workspace = await db.scalar(
                select(Workspace).where(Workspace.normalized_path == normalized)
            )
        if workspace is not None:
            # Backfill semantics for rows created before classification was
            # introduced.  ``setattr`` is guarded for compatibility with
            # pre-migration models/databases.
            workspace_kind, workspace_confidence = self.classify_workspace(normalized)
            if hasattr(workspace, "workspace_kind") and getattr(
                workspace, "workspace_kind", None
            ) in (None, "", "unknown"):
                setattr(workspace, "workspace_kind", workspace_kind)  # noqa: B010
            if hasattr(workspace, "workspace_confidence") and getattr(
                workspace, "workspace_confidence", None
            ) in (None, "", "low"):
                setattr(  # noqa: B010
                    workspace, "workspace_confidence", workspace_confidence
                )
            session.workspace_id = workspace.id


__all__ = ["SessionLinker"]
