"""LANtern Minecraft UI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from .admin_session import AdminSessionAccess, LoginRateLimiter
from .schematic_workspace import (
    HttpViewerAdapter,
    ViewerAdapter,
    ViewerConnectionError,
    ViewerIncompatible,
    ViewerTimeout,
    install_schematic_workspace,
    viewer_readiness,
)
from .settings import Settings


class LoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


STATIC = Path(__file__).parent.parent / "static"
MAX_LOGIN_BODY_BYTES = 4 * 1024


async def _read_login_body(request: Request) -> LoginBody:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise HTTPException(400, "invalid Content-Length") from exc
        if declared_length < 0:
            raise HTTPException(400, "invalid Content-Length")
        if declared_length > MAX_LOGIN_BODY_BYTES:
            raise HTTPException(413, "login request is too large")

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_LOGIN_BODY_BYTES:
            raise HTTPException(413, "login request is too large")
    try:
        return LoginBody.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(422, "invalid login request") from exc


def create_app(
    *, viewer: ViewerAdapter | None = None, settings: Settings | None = None
) -> FastAPI:
    config = settings or Settings.from_environment()
    access = AdminSessionAccess.from_settings(config)
    login_limiter = LoginRateLimiter()
    active_viewer = viewer or HttpViewerAdapter(config.viewer_url)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        close = getattr(active_viewer, "aclose", None)
        if close:
            await close()

    app = FastAPI(
        title="LANtern Minecraft",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def browser_security(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/session"):
            response.headers["Cache-Control"] = "no-store"
        if not request.url.path.startswith("/schematics/"):
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("Referrer-Policy", "no-referrer")
            response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
            response.headers.setdefault(
                "Permissions-Policy",
                "camera=(), microphone=(), geolocation=(), payment=()",
            )
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; frame-src 'self'; object-src 'none'; "
                "base-uri 'none'; form-action 'self'",
            )
        return response

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def ready():
        try:
            descriptor = await viewer_readiness(active_viewer)
        except (ViewerConnectionError, ViewerIncompatible, ViewerTimeout):
            return JSONResponse(
                {"status": "not-ready", "detail": "schematic viewer is unavailable"},
                status_code=503,
            )
        return {"status": "ready", "viewer": descriptor}

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/session")
    async def session_status(request: Request) -> dict[str, bool]:
        return {"enabled": access.enabled, "authenticated": access.is_admin(request)}

    @app.post("/api/session/login", status_code=204)
    async def login(request: Request, response: Response) -> None:
        access.require_same_origin(request)
        if not access.enabled:
            raise HTTPException(503, "administrator login is not configured")
        client_host = request.client.host if request.client else "unknown"
        retry_after = login_limiter.retry_after(client_host)
        if retry_after is not None:
            raise HTTPException(
                429,
                "too many failed login attempts",
                headers={"Retry-After": str(retry_after)},
            )
        body = await _read_login_body(request)
        if not access.verify_password(body.password):
            login_limiter.failed(client_host)
            raise HTTPException(401, "invalid administrator credentials")
        login_limiter.succeeded(client_host)
        access.issue(response)

    @app.post("/api/session/logout", status_code=204)
    async def logout(request: Request, response: Response) -> None:
        access.require_same_origin(request)
        access.clear(response)

    install_schematic_workspace(app, active_viewer, access)
    app.mount(
        "/static",
        StaticFiles(directory=STATIC, check_dir=False),
        name="static",
    )

    return app


app = create_app()
