"""FastAPI application.

- request-id middleware stamps every request with a correlation id (used by
  `audit_logs.request_id`).
- access guard: there is no login (single local user), so the API protects
  itself with a Host allowlist (DNS rebinding) and a same-origin check on
  every state-changing request (cross-site request forgery).

All routers mount here.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError

from agent_observatory import __version__
from agent_observatory.api import audit as audit_api
from agent_observatory.api import history as history_api
from agent_observatory.api import insights as insights_api
from agent_observatory.api import memories as memories_api
from agent_observatory.api import projects as projects_api
from agent_observatory.api import retention as retention_api
from agent_observatory.api import system as system_api
from agent_observatory.api.health import router as health_router
from agent_observatory.config import Settings, settings

logger = logging.getLogger(__name__)

#: Methods that never change state; everything else must be same-origin.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})


def _hostname(value: str | None) -> str | None:
    """Lower-cased host of a ``Host`` header value (port and brackets dropped)."""
    if not value:
        return None
    try:
        return urlsplit(f"//{value}").hostname
    except ValueError:
        return None


def allowed_host_names(config: Settings) -> frozenset[str] | None:
    """Host names the API answers to; ``None`` accepts any.

    Bound to loopback (the default), only loopback names are accepted, so a
    web page that rebinds its own domain to 127.0.0.1 cannot read the API.
    ``AGENT_OBS_ALLOWED_HOSTS`` adds names (``*`` accepts all); with no list,
    a non-loopback bind such as ``0.0.0.0`` is an explicit choice to serve the
    network and accepts any name.
    """
    explicit = {name.strip().lower() for name in config.allowed_hosts}
    if "*" in explicit:
        return None
    if explicit:
        return frozenset(explicit | _LOOPBACK_NAMES)
    bind = config.host.strip().lower().strip("[]")
    if bind in _LOOPBACK_NAMES or bind.startswith("127."):
        return _LOOPBACK_NAMES
    return None


def cross_site_reason(method: str, host: str | None, headers) -> str | None:
    """Why a state-changing request must be refused, or ``None``.

    Browsers attach ``Origin`` to every cross-origin POST/PUT/PATCH/DELETE, so
    a request whose origin is not this very host:port came from another site.
    Requests without ``Origin`` (curl, the CLI) are accepted unless the
    browser marked them ``Sec-Fetch-Site: cross-site``.
    """
    if method.upper() in _SAFE_METHODS:
        return None
    origin = headers.get("origin")
    if origin is not None:
        if origin == "null":
            return "opaque origin"
        try:
            netloc = urlsplit(origin).netloc.lower()
        except ValueError:
            return "malformed origin"
        if not host or netloc != host.lower():
            return "cross-origin request"
        return None
    if (headers.get("sec-fetch-site") or "").lower() == "cross-site":
        return "cross-site request"
    return None


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings().app_name,
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redirect_slashes=False,
    )

    def error_response(
        request: Request,
        *,
        status_code: int,
        code: str,
        message: str,
        retryable: bool = False,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status_code,
            content={
                "error": {
                    "code": code,
                    "message": message,
                    "request_id": getattr(request.state, "request_id", None),
                    "retryable": retryable,
                }
            },
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        code = {
            400: "bad_request",
            403: "permission_denied",
            404: "not_found",
            409: "conflict",
            422: "validation_error",
        }.get(exc.status_code, f"http_{exc.status_code}")
        return error_response(
            request,
            status_code=exc.status_code,
            code=code,
            message=str(exc.detail),
            retryable=exc.status_code >= 500,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, _exc: RequestValidationError):
        return error_response(
            request,
            status_code=422,
            code="validation_error",
            message="The request payload is invalid.",
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError):
        logger.exception(
            "database request error request_id=%s path=%s",
            getattr(request.state, "request_id", None),
            request.url.path,
            exc_info=exc,
        )
        return error_response(
            request,
            status_code=503,
            code="database_unavailable",
            message="PostgreSQL is unavailable.",
            retryable=True,
        )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        logger.exception(
            "unhandled request error request_id=%s path=%s",
            getattr(request.state, "request_id", None),
            request.url.path,
            exc_info=exc,
        )
        return error_response(
            request,
            status_code=500,
            code="internal_error",
            message="The server could not complete the request.",
            retryable=True,
        )

    @app.middleware("http")
    async def access_guard(request: Request, call_next):
        config = settings()
        host = request.headers.get("host")
        allowed = allowed_host_names(config)
        if allowed is not None and _hostname(host) not in allowed:
            logger.warning("refused request for host %r path=%s", host, request.url.path)
            return error_response(
                request,
                status_code=403,
                code="host_not_allowed",
                message=(
                    "This host name is not allowed. Open the observatory via 127.0.0.1 or "
                    "localhost, or list the name in AGENT_OBS_ALLOWED_HOSTS."
                ),
            )
        reason = cross_site_reason(request.method, host, request.headers)
        if reason is not None:
            logger.warning(
                "refused %s %s: %s (origin=%r)",
                request.method,
                request.url.path,
                reason,
                request.headers.get("origin"),
            )
            return error_response(
                request,
                status_code=403,
                code="cross_origin_refused",
                message="State-changing requests must come from the observatory itself.",
            )
        return await call_next(request)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        if request.url.path.startswith("/api/"):
            # API data is live local state; a browser-cached copy would show
            # pre-cleanup numbers after a reload.
            response.headers.setdefault("cache-control", "no-store")
        return response

    app.include_router(health_router, prefix="/api", tags=["health"])
    app.include_router(history_api.router, tags=["codex-history"])
    app.include_router(insights_api.router, tags=["insights"])
    app.include_router(memories_api.router, tags=["memories"])
    app.include_router(projects_api.router, tags=["projects"])
    app.include_router(audit_api.router, tags=["audit"])
    app.include_router(system_api.router, tags=["system"])
    app.include_router(retention_api.router, tags=["retention"])

    # Stable mode serves the built Vue application from the same origin.  The
    # development Vite server remains available via `agent-observatory dev`.
    frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if frontend_dist.is_dir():
        app.mount("/assets", StaticFiles(directory=frontend_dist / "assets"), name="assets")

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon():
        icon = frontend_dist / "favicon.svg"
        if icon.is_file():
            return FileResponse(icon, media_type="image/svg+xml")
        raise HTTPException(status_code=404, detail="Not found")

    @app.get("/", include_in_schema=False)
    async def root():
        index = frontend_dist / "index.html"
        if index.is_file():
            return FileResponse(index)
        return {"name": settings().app_name, "version": __version__}

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend_fallback(path: str, request: Request):
        if path.startswith("api/"):
            return error_response(
                request,
                status_code=404,
                code="not_found",
                message="Not found",
            )
        index = frontend_dist / "index.html"
        if index.is_file():
            return FileResponse(index)
        return JSONResponse(status_code=404, content={"detail": "Not found"})

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "agent_observatory.main:app",
        host=settings().host,
        port=settings().port,
        reload=False,
    )
