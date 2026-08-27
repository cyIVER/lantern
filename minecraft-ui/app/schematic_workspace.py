"""Same-origin gateway for the private Create Schematic Viewer sidecar."""

from __future__ import annotations

import json
from collections.abc import AsyncIterable, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from starlette.background import BackgroundTask

from .admin_session import AdminSessionAccess

_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
MAX_UPLOAD_BYTES = 250 * 1024 * 1024
_REQUEST_HEADERS_TO_REMOVE = {
    "authorization",
    "connection",
    "cookie",
    "forwarded",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "x-lantern-schematic-admin",
}
_RESPONSE_HEADERS_TO_REMOVE = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "set-cookie",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


@dataclass(slots=True)
class ProxyRequest:
    method: str
    path: str
    query: bytes
    headers: Mapping[str, str]
    body: AsyncIterable[bytes]


@dataclass(slots=True)
class ProxyResponse:
    status_code: int
    headers: list[tuple[str, str]]
    body: AsyncIterable[bytes]
    close: Callable[[], Awaitable[None]] | None = field(default=None, repr=False)


class ViewerAdapter(Protocol):
    async def exchange(self, request: ProxyRequest) -> ProxyResponse: ...


class ViewerConnectionError(RuntimeError):
    """The fixed private viewer upstream could not be reached."""


class ViewerTimeout(RuntimeError):
    """The private viewer exceeded the bounded conversion timeout."""


class UploadTooLarge(RuntimeError):
    """The streamed request exceeded the viewer contract limit."""


class HttpViewerAdapter:
    """Streaming production adapter for the fixed private viewer service."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            timeout=httpx.Timeout(connect=5, read=300, write=300, pool=5),
        )

    async def exchange(self, request: ProxyRequest) -> ProxyResponse:
        url = f"{self._base_url}{request.path}"
        if request.query:
            url = f"{url}?{request.query.decode('ascii')}"
        upstream_request = self._client.build_request(
            request.method,
            url,
            headers=dict(request.headers),
            content=request.body,
        )
        try:
            response = await self._client.send(upstream_request, stream=True)
        except httpx.TimeoutException as exc:
            raise ViewerTimeout from exc
        except httpx.RequestError as exc:
            raise ViewerConnectionError from exc
        return ProxyResponse(
            status_code=response.status_code,
            headers=list(response.headers.multi_items()),
            body=response.aiter_raw(),
            close=response.aclose,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class ViewerIncompatible(RuntimeError):
    """The private sidecar does not satisfy the LANtern embed contract."""


async def _limited_body(body: AsyncIterable[bytes]) -> AsyncIterable[bytes]:
    received = 0
    async for chunk in body:
        received += len(chunk)
        if received > MAX_UPLOAD_BYTES:
            raise UploadTooLarge
        yield chunk


async def _read_probe(response: ProxyResponse) -> dict[str, object]:
    collected = bytearray()
    try:
        async for chunk in response.body:
            collected.extend(chunk)
            if len(collected) > 64 * 1024:
                raise ViewerIncompatible("probe response is too large")
    finally:
        if response.close:
            await response.close()
    try:
        value = json.loads(collected)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ViewerIncompatible("probe response is not JSON") from exc
    if not isinstance(value, dict):
        raise ViewerIncompatible("probe response is not an object")
    return value


async def viewer_readiness(viewer: ViewerAdapter) -> dict[str, object]:
    empty = _empty_body()
    ready = await viewer.exchange(ProxyRequest("GET", "/readyz", b"", {}, empty))
    if ready.status_code != 200:
        if ready.close:
            await ready.close()
        raise ViewerIncompatible("viewer is not ready")
    await _read_probe(ready)
    capabilities = await viewer.exchange(
        ProxyRequest("GET", "/api/v1/capabilities", b"", {}, _empty_body())
    )
    if capabilities.status_code != 200:
        if capabilities.close:
            await capabilities.close()
        raise ViewerIncompatible("capabilities are unavailable")
    descriptor = await _read_probe(capabilities)
    if descriptor.get("application") != "create-schematic-viewer":
        raise ViewerIncompatible("unexpected viewer application")
    if descriptor.get("contractVersion") != 1:
        raise ViewerIncompatible("unsupported viewer contract")
    return {
        "application": descriptor["application"],
        "contractVersion": descriptor["contractVersion"],
    }


async def _empty_body() -> AsyncIterable[bytes]:
    if False:  # pragma: no cover - preserves the async-iterator interface
        yield b""


class UnavailableViewer:
    async def exchange(self, _request: ProxyRequest) -> ProxyResponse:
        async def message() -> AsyncIterable[bytes]:
            yield b'{"detail":"schematic viewer is not configured"}'

        return ProxyResponse(
            status_code=503,
            headers=[("content-type", "application/json")],
            body=message(),
        )


def _request_headers(request: Request) -> dict[str, str]:
    clean: dict[str, str] = {}
    for name, value in request.headers.items():
        lower = name.lower()
        if lower in _REQUEST_HEADERS_TO_REMOVE or lower.startswith("x-forwarded-"):
            continue
        clean[lower] = value
    return clean


def _response_headers(headers: list[tuple[str, str]]) -> dict[str, str]:
    return {
        name: value
        for name, value in headers
        if name.lower() not in _RESPONSE_HEADERS_TO_REMOVE
    }


def install_schematic_workspace(
    app: FastAPI, viewer: ViewerAdapter, access: AdminSessionAccess
) -> None:
    @app.get("/schematics", include_in_schema=False)
    async def schematics_redirect() -> RedirectResponse:
        return RedirectResponse("/schematics/", status_code=308)

    @app.api_route(
        "/schematics/{viewer_path:path}",
        methods=_METHODS,
        include_in_schema=False,
    )
    async def schematic_proxy(request: Request, viewer_path: str):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                declared_length = int(content_length)
            except ValueError as exc:
                raise HTTPException(400, "invalid Content-Length") from exc
            if declared_length < 0:
                raise HTTPException(400, "invalid Content-Length")
            if declared_length > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "schematic upload exceeds 250 MiB")
        raw_path = request.scope.get("raw_path", request.url.path.encode("ascii"))
        prefix = b"/schematics"
        if not raw_path.startswith(prefix):  # Defensive: routing should make this impossible.
            raise HTTPException(400, "invalid schematic path")
        path = raw_path[len(prefix) :].decode("ascii") or "/"
        headers = _request_headers(request)
        credential = access.viewer_credential(request, path)
        if credential:
            headers["x-lantern-schematic-admin"] = credential
        try:
            upstream = await viewer.exchange(
                ProxyRequest(
                    method=request.method,
                    path=path,
                    query=request.url.query.encode("ascii"),
                    headers=headers,
                    body=_limited_body(request.stream()),
                )
            )
        except UploadTooLarge:
            return JSONResponse(
                {"detail": "schematic upload exceeds 250 MiB"}, status_code=413
            )
        except ViewerTimeout:
            return JSONResponse(
                {"detail": "schematic conversion timed out"}, status_code=504
            )
        except ViewerConnectionError:
            return JSONResponse(
                {"detail": "schematic viewer is unavailable"}, status_code=502
            )
        response_headers = _response_headers(upstream.headers)
        location = response_headers.get("location")
        if location and location.startswith("/"):
            response_headers["location"] = f"/schematics{location}"
        background = BackgroundTask(upstream.close) if upstream.close else None
        return StreamingResponse(
            upstream.body,
            status_code=upstream.status_code,
            headers=response_headers,
            background=background,
        )
