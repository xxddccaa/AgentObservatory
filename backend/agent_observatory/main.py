"""FastAPI application.

Phase 1 adds the auth surface on top of the Phase 0 health check:

- request-id middleware stamps every request with a correlation id (used by
  `audit_logs.request_id`).
- startup role seed guarantees `admin`/`operator`/`viewer` always resolve.
- auth router (`/api/auth/*`) and admin router (`/api/admin/*`).

All later routers mount here as they land.
"""
from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError

from agent_observatory import __version__
from agent_observatory.api import admin as admin_api
from agent_observatory.api import audit as audit_api
from agent_observatory.api import auth as auth_api
from agent_observatory.api import history as history_api
from agent_observatory.api import insights as insights_api
from agent_observatory.api import memories as memories_api
from agent_observatory.api import projects as projects_api
from agent_observatory.api import retention as retention_api
from agent_observatory.api import stats as stats_api
from agent_observatory.api import system as system_api
from agent_observatory.api.health import router as health_router
from agent_observatory.config import settings
from agent_observatory.db.session import get_engine

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Seed baseline roles at startup so RBAC checks never hit a missing row.
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import sessionmaker

    engine = get_engine()
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        from agent_observatory.services import users

        await users.seed_roles(session)
        await session.commit()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings().app_name,
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redirect_slashes=False,
        lifespan=lifespan,
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
            401: "authentication_required",
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
    app.include_router(auth_api.router, tags=["auth"])
    app.include_router(admin_api.router, tags=["admin"])
    app.include_router(history_api.router, tags=["codex-history"])
    app.include_router(insights_api.router, tags=["insights"])
    app.include_router(memories_api.router, tags=["memories"])
    app.include_router(projects_api.router, tags=["projects"])
    app.include_router(stats_api.router, tags=["stats"])
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
