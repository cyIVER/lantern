"""LANtern Minecraft UI application factory."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, unquote

import anyio
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from .admin_session import AdminSessionAccess, LoginRateLimiter
from .identity import COOKIE_NAME, IdentityError
from .portal import NamedViewerAccess
from .runtime import PortalRuntime, build_runtime
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
MAX_INTENT_BODY_BYTES = 64 * 1024
MAX_SCHEMATIC_SUBMISSION_BYTES = 64 * 1024 * 1024
MAX_MOD_UPLOAD_BYTES = 128 * 1024 * 1024
MAX_METADATA_HEADER_BYTES = 16 * 1024


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


async def _read_intent_body(request: Request) -> dict:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise HTTPException(400, "invalid Content-Length") from exc
        if declared_length < 0:
            raise HTTPException(400, "invalid Content-Length")
        if declared_length > MAX_INTENT_BODY_BYTES:
            raise HTTPException(413, "portal intent is too large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_INTENT_BODY_BYTES:
            raise HTTPException(413, "portal intent is too large")
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(422, "invalid portal intent") from exc
    if not isinstance(value, dict):
        raise HTTPException(422, "invalid portal intent")
    return value


async def _read_binary_body(request: Request, *, limit: int, label: str) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise HTTPException(400, "invalid Content-Length") from exc
        if declared_length < 0:
            raise HTTPException(400, "invalid Content-Length")
        if declared_length > limit:
            raise HTTPException(413, f"{label} is too large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(413, f"{label} is too large")
    if not body:
        raise HTTPException(422, f"{label} is empty")
    return bytes(body)


def _submission_metadata(request: Request) -> dict:
    encoded = request.headers.get("x-schematic-metadata", "")
    if len(encoded.encode("utf-8")) > MAX_METADATA_HEADER_BYTES:
        raise HTTPException(413, "schematic metadata is too large")
    if not encoded:
        return {}
    try:
        value = json.loads(unquote(encoded))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(422, "invalid schematic metadata") from exc
    if not isinstance(value, dict):
        raise HTTPException(422, "invalid schematic metadata")
    return value


def create_app(
    *,
    viewer: ViewerAdapter | None = None,
    settings: Settings | None = None,
    runtime: PortalRuntime | None = None,
) -> FastAPI:
    """Create the LANtern Minecraft FastAPI application with schematic workspace and admin session."""
    config = settings or Settings.from_environment()
    active_runtime = runtime or build_runtime(config)
    access = active_runtime.viewer_access
    portal = active_runtime.portal
    login_limiter = LoginRateLimiter()
    active_viewer = viewer or HttpViewerAdapter(config.viewer_url)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        close = getattr(active_viewer, "aclose", None)
        if close:
            await close()
        await active_runtime.aclose()

    app = FastAPI(
        title="LANtern Minecraft",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def browser_security(request: Request, call_next):
        if portal.identity is not None:
            try:
                portal.require_trusted_host(request)
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
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

    @app.get("/api/workspace")
    async def workspace(request: Request) -> dict:
        return await portal.workspace(request)

    @app.post("/api/intents")
    async def execute_intent(request: Request, response: Response) -> dict:
        return await portal.execute(request, response, await _read_intent_body(request))

    @app.post("/api/submissions")
    async def submit_schematic(request: Request) -> dict:
        portal.require_same_origin(request)
        filename = request.headers.get("x-schematic-filename", "")
        if not filename:
            raise HTTPException(422, "schematic filename is required")
        promote = request.headers.get("x-schematic-promote", "false").lower() in {
            "1",
            "true",
            "yes",
        }
        admission_id = await portal.admit_schematic_upload(
            request, maximum_body_bytes=MAX_SCHEMATIC_SUBMISSION_BYTES
        )
        try:
            content = await _read_binary_body(
                request, limit=MAX_SCHEMATIC_SUBMISSION_BYTES, label="schematic submission"
            )
            return await portal.submit_schematic(
                request,
                filename=filename,
                content=content,
                promote=promote,
                metadata=_submission_metadata(request),
                admission_id=admission_id,
            )
        finally:
            await portal.cancel_schematic_upload(admission_id)

    @app.post("/api/admin/mods")
    async def stage_mod(request: Request) -> dict:
        portal.require_mod_upload(request)
        filename = request.headers.get("x-mod-filename", "")
        idempotency_key = request.headers.get("idempotency-key", "")
        if not filename:
            raise HTTPException(422, "mod filename is required")
        content = await _read_binary_body(request, limit=MAX_MOD_UPLOAD_BYTES, label="mod upload")
        return await portal.stage_mod(
            request,
            filename=filename,
            content=content,
            idempotency_key=idempotency_key,
        )

    @app.get("/api/admin/submissions/{submission_id}/download")
    async def download_submission(request: Request, submission_id: str) -> Response:
        filename, content = await portal.download_submission(request, submission_id)
        encoded_filename = quote(filename, safe="")
        return Response(
            content,
            media_type="application/octet-stream",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/session/login", status_code=204)
    async def login(request: Request, response: Response) -> None:
        if not isinstance(access, AdminSessionAccess):
            raise HTTPException(410, "use named administrator sign-in")
        access.require_same_origin(request)
        if not access.enabled:
            raise HTTPException(503, "administrator login is disabled")
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
        if isinstance(access, AdminSessionAccess):
            access.require_same_origin(request)
            access.clear(response)
        else:
            access.require_same_origin(request)
            if isinstance(access, NamedViewerAccess):
                try:
                    await anyio.to_thread.run_sync(
                        access.revoke, request.cookies.get(COOKIE_NAME, "")
                    )
                except IdentityError:
                    pass
            response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict")

    install_schematic_workspace(app, active_viewer, access)
    app.mount(
        "/static",
        StaticFiles(directory=STATIC, check_dir=False),
        name="static",
    )

    return app


app = create_app()
