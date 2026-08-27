import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app.main import create_app
from app.schematic_workspace import (
    HttpViewerAdapter,
    ProxyRequest,
    ProxyResponse,
    ViewerConnectionError,
    ViewerTimeout,
)
from app.settings import Settings


async def _chunks(*values: bytes) -> AsyncIterator[bytes]:
    for value in values:
        yield value


class RecordingViewer:
    def __init__(
        self,
        *,
        extra_response_headers: list[tuple[str, str]] | None = None,
        protect_library_mutations: bool = False,
    ) -> None:
        self.requests: list[ProxyRequest] = []
        self.extra_response_headers = extra_response_headers or []
        self.protect_library_mutations = protect_library_mutations

    async def exchange(self, request: ProxyRequest) -> ProxyResponse:
        self.requests.append(request)
        if (
            self.protect_library_mutations
            and request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and request.path.startswith("/api/v1/library/")
            and "x-lantern-schematic-admin" not in request.headers
        ):
            return ProxyResponse(
                status_code=403,
                headers=[("content-type", "application/json")],
                body=_chunks(b'{"error":"administrator access required"}'),
            )
        return ProxyResponse(
            status_code=206,
            headers=[
                ("content-type", "application/octet-stream"),
                ("content-disposition", 'attachment; filename="build.nbt"'),
                ("x-schematic-converter", "litematic"),
                *self.extra_response_headers,
            ],
            body=_chunks(b"first-", b"second"),
        )


class UnreachableViewer:
    async def exchange(self, _request: ProxyRequest) -> ProxyResponse:
        raise ViewerConnectionError("http://schematic-viewer:4173 leaked detail")


class CompatibleViewer:
    async def exchange(self, request: ProxyRequest) -> ProxyResponse:
        if request.path == "/readyz":
            payload = {"status": "ready"}
        elif request.path == "/api/v1/capabilities":
            payload = {"application": "create-schematic-viewer", "contractVersion": 1}
        else:
            raise AssertionError(f"unexpected readiness request: {request.path}")
        return ProxyResponse(
            status_code=200,
            headers=[("content-type", "application/json")],
            body=_chunks(json.dumps(payload).encode()),
        )


class TimedOutViewer:
    async def exchange(self, _request: ProxyRequest) -> ProxyResponse:
        raise ViewerTimeout("private timeout detail")


class ClosingFailingViewer:
    def __init__(self) -> None:
        self.closed = False

    async def exchange(self, _request: ProxyRequest) -> ProxyResponse:
        async def failing_body() -> AsyncIterator[bytes]:
            yield b"partial"
            raise RuntimeError("upstream stream failed")

        async def close() -> None:
            self.closed = True

        return ProxyResponse(
            status_code=200,
            headers=[("content-type", "application/octet-stream")],
            body=failing_body(),
            close=close,
        )


def test_schematics_without_trailing_slash_redirects_permanently() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/schematics", follow_redirects=False)

    assert response.status_code == 308
    assert response.headers["location"] == "/schematics/"


def test_schematic_workspace_preserves_path_query_binary_body_and_metadata() -> None:
    viewer = RecordingViewer()
    with TestClient(create_app(viewer=viewer)) as client:
        response = client.get(
            "/schematics/api/v1/download?id=7",
            headers={
                "Forwarded": "for=attacker",
                "X-Forwarded-For": "attacker",
                "X-Lantern-Schematic-Admin": "forged",
            },
        )

    assert response.status_code == 206
    assert response.content == b"first-second"
    assert response.headers["content-disposition"] == 'attachment; filename="build.nbt"'
    assert response.headers["x-schematic-converter"] == "litematic"
    assert len(viewer.requests) == 1
    forwarded = viewer.requests[0]
    assert forwarded.path == "/api/v1/download"
    assert forwarded.query == b"id=7"
    assert "forwarded" not in forwarded.headers
    assert "x-forwarded-for" not in forwarded.headers
    assert "x-lantern-schematic-admin" not in forwarded.headers


def _admin_settings(tmp_path: Path, *, allow_insecure_admin: bool) -> Settings:
    password_hash = tmp_path / "admin-password-hash"
    session_secret = tmp_path / "session-secret"
    viewer_token = tmp_path / "viewer-token"
    password_hash.write_text(PasswordHasher().hash("correct horse battery staple"))
    session_secret.write_bytes(b"s" * 32)
    viewer_token.write_bytes(b"v" * 32)
    return Settings(
        admin_password_hash_file=password_hash,
        session_secret_file=session_secret,
        viewer_admin_token_file=viewer_token,
        allow_insecure_admin=allow_insecure_admin,
    )


def test_plaintext_admin_is_disabled_by_default_while_anonymous_browsing_remains(
    tmp_path: Path,
) -> None:
    viewer = RecordingViewer(protect_library_mutations=True)
    settings = _admin_settings(tmp_path, allow_insecure_admin=False)

    with TestClient(create_app(viewer=viewer, settings=settings)) as client:
        session = client.get("/api/session")
        login = client.post(
            "/api/session/login",
            headers={"Origin": "http://testserver"},
            json={"password": "correct horse battery staple"},
        )
        browse = client.get("/schematics/api/v1/library/schematics")
        mutation = client.post(
            "/schematics/api/v1/library/schematics",
            headers={"Origin": "http://testserver"},
            content=b"{}",
        )

    assert session.json() == {"enabled": False, "authenticated": False}
    assert login.status_code == 503
    assert login.json() == {"detail": "administrator login is disabled"}
    assert "set-cookie" not in login.headers
    assert browse.status_code == 206
    assert mutation.status_code == 403
    assert len(viewer.requests) == 2
    assert "x-lantern-schematic-admin" not in viewer.requests[-1].headers


def test_plaintext_default_rejects_a_previously_signed_admin_cookie(tmp_path: Path) -> None:
    permitted = _admin_settings(tmp_path, allow_insecure_admin=True)
    with TestClient(create_app(viewer=RecordingViewer(), settings=permitted)) as client:
        login = client.post(
            "/api/session/login",
            headers={"Origin": "http://testserver"},
            json={"password": "correct horse battery staple"},
        )
        signed_cookie = client.cookies["lantern_minecraft_admin"]

    viewer = RecordingViewer(protect_library_mutations=True)
    locked = replace(permitted, allow_insecure_admin=False)
    with TestClient(create_app(viewer=viewer, settings=locked)) as client:
        client.cookies.set("lantern_minecraft_admin", signed_cookie)
        session = client.get("/api/session")
        mutation = client.post(
            "/schematics/api/v1/library/schematics",
            headers={"Origin": "http://testserver"},
            content=b"{}",
        )

    assert login.status_code == 204
    assert session.json() == {"enabled": False, "authenticated": False}
    assert mutation.status_code == 403
    assert "x-lantern-schematic-admin" not in viewer.requests[-1].headers


def test_secure_cookie_configuration_enables_admin_without_insecure_override(
    tmp_path: Path,
) -> None:
    viewer = RecordingViewer(protect_library_mutations=True)
    settings = replace(
        _admin_settings(tmp_path, allow_insecure_admin=False), secure_cookie=True
    )
    with TestClient(
        create_app(viewer=viewer, settings=settings), base_url="https://testserver"
    ) as client:
        login = client.post(
            "/api/session/login",
            headers={"Origin": "https://testserver"},
            json={"password": "correct horse battery staple"},
        )
        mutation = client.post(
            "/schematics/api/v1/library/schematics",
            headers={"Origin": "https://testserver"},
            content=b"{}",
        )

    assert login.status_code == 204
    assert "Secure" in login.headers["set-cookie"]
    assert mutation.status_code == 206
    assert viewer.requests[-1].headers["x-lantern-schematic-admin"] == "v" * 32


def test_insecure_admin_environment_override_is_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MINECRAFT_ALLOW_INSECURE_ADMIN", raising=False)
    assert Settings.from_environment().allow_insecure_admin is False

    monkeypatch.setenv("MINECRAFT_ALLOW_INSECURE_ADMIN", "true")
    assert Settings.from_environment().allow_insecure_admin is True


def test_explicit_insecure_admin_override_allows_test_mutation(tmp_path: Path) -> None:
    viewer = RecordingViewer(protect_library_mutations=True)
    with TestClient(
        create_app(
            viewer=viewer,
            settings=_admin_settings(tmp_path, allow_insecure_admin=True),
        )
    ) as client:
        login = client.post(
            "/api/session/login",
            headers={"Origin": "http://testserver"},
            json={"password": "correct horse battery staple"},
        )
        mutation = client.post(
            "/schematics/api/v1/library/schematics",
            headers={"Origin": "http://testserver"},
            content=b"{}",
        )

    assert login.status_code == 204
    assert "HttpOnly" in login.headers["set-cookie"]
    assert "SameSite=strict" in login.headers["set-cookie"]
    assert mutation.status_code == 206
    assert viewer.requests[-1].headers["x-lantern-schematic-admin"] == "v" * 32
    assert "v" * 32 not in mutation.text


def test_unsafe_schematic_request_requires_exact_origin() -> None:
    viewer = RecordingViewer()
    with TestClient(create_app(viewer=viewer)) as client:
        response = client.post(
            "/schematics/api/convert",
            headers={"Origin": "http://other-host:8093"},
            content=b"schematic",
        )

    assert response.status_code == 403
    assert viewer.requests == []


def test_schematic_workspace_isolates_browser_and_sidecar_credentials() -> None:
    viewer = RecordingViewer(extra_response_headers=[("set-cookie", "viewer=leak")])
    with TestClient(create_app(viewer=viewer)) as client:
        response = client.get(
            "/schematics/",
            headers={
                "Authorization": "Bearer browser-secret",
                "Cookie": "unrelated=session",
                "Proxy-Authorization": "Basic proxy-secret",
            },
        )

    assert "authorization" not in viewer.requests[0].headers
    assert "cookie" not in viewer.requests[0].headers
    assert "proxy-authorization" not in viewer.requests[0].headers
    assert "set-cookie" not in response.headers


def test_unreachable_viewer_returns_sanitized_bad_gateway() -> None:
    with TestClient(
        create_app(viewer=UnreachableViewer()), raise_server_exceptions=False
    ) as client:
        response = client.get("/schematics/")

    assert response.status_code == 502
    assert response.json() == {"detail": "schematic viewer is unavailable"}
    assert "4173" not in response.text


def test_declared_upload_over_250_mib_is_rejected_before_upstream() -> None:
    viewer = RecordingViewer()
    with TestClient(create_app(viewer=viewer)) as client:
        response = client.post(
            "/schematics/api/convert",
            headers={
                "Origin": "http://testserver",
                "Content-Length": str(250 * 1024 * 1024 + 1),
            },
            content=b"small test body",
        )

    assert response.status_code == 413
    assert response.json() == {"detail": "schematic upload exceeds 250 MiB"}
    assert viewer.requests == []


def test_readiness_requires_compatible_ready_viewer() -> None:
    with TestClient(create_app(viewer=CompatibleViewer())) as client:
        health = client.get("/healthz")
        readiness = client.get("/readyz")

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert readiness.status_code == 200
    assert readiness.json() == {
        "status": "ready",
        "viewer": {"application": "create-schematic-viewer", "contractVersion": 1},
    }


def test_http_viewer_adapter_streams_to_fixed_private_upstream() -> None:
    observed: dict[str, object] = {}

    class UpstreamStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"converted"

    async def upstream(request: httpx.Request) -> httpx.Response:
        observed["url"] = str(request.url)
        observed["body"] = await request.aread()
        return httpx.Response(
            201,
            headers={"Content-Type": "application/octet-stream"},
            stream=UpstreamStream(),
        )

    viewer = HttpViewerAdapter(
        "http://schematic-viewer:4173", transport=httpx.MockTransport(upstream)
    )
    with TestClient(create_app(viewer=viewer)) as client:
        response = client.post(
            "/schematics/api/convert?format=nbt",
            headers={"Origin": "http://testserver"},
            content=b"input-schematic",
        )

    assert response.status_code == 201
    assert response.content == b"converted"
    assert observed == {
        "url": "http://schematic-viewer:4173/api/convert?format=nbt",
        "body": b"input-schematic",
    }


def test_failed_admin_logins_are_rate_limited_without_trusting_forwarded_ip(
    tmp_path: Path,
) -> None:
    with TestClient(
        create_app(
            viewer=RecordingViewer(),
            settings=_admin_settings(tmp_path, allow_insecure_admin=True),
        )
    ) as client:
        for attempt in range(5):
            response = client.post(
                "/api/session/login",
                headers={
                    "Origin": "http://testserver",
                    "X-Forwarded-For": f"spoof-{attempt}",
                },
                json={"password": "wrong"},
            )
            assert response.status_code == 401
        blocked = client.post(
            "/api/session/login",
            headers={
                "Origin": "http://testserver",
                "X-Forwarded-For": "brand-new-spoof",
            },
            json={"password": "correct horse battery staple"},
        )

    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "300"
    assert "set-cookie" not in blocked.headers


def test_admin_login_rejects_oversized_body_before_json_parsing(tmp_path: Path) -> None:
    with TestClient(
        create_app(
            viewer=RecordingViewer(),
            settings=_admin_settings(tmp_path, allow_insecure_admin=True),
        )
    ) as client:
        response = client.post(
            "/api/session/login",
            headers={"Origin": "http://testserver"},
            content=b"{" + b"x" * 4096,
        )

    assert response.status_code == 413
    assert response.json() == {"detail": "login request is too large"}


def test_admin_login_stream_limit_does_not_depend_on_content_length(
    tmp_path: Path,
) -> None:
    async def send_chunked() -> httpx.Response:
        transport = httpx.ASGITransport(
            app=create_app(
                viewer=RecordingViewer(),
                settings=_admin_settings(tmp_path, allow_insecure_admin=True),
            )
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.post(
                "/api/session/login",
                headers={"Origin": "http://testserver"},
                content=_chunks(b'{"password":"', b"x" * 4096, b'"}'),
            )

    response = asyncio.run(send_chunked())

    assert response.status_code == 413
    assert response.json() == {"detail": "login request is too large"}


def test_minecraft_home_exposes_overview_and_persistent_schematic_workspace() -> None:
    with TestClient(create_app(viewer=RecordingViewer())) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert 'role="tab"' in response.text
    assert 'href="/schematics/"' in response.text
    assert 'title="Schematic library and 3D viewer"' in response.text
    assert 'id="schematic-frame"' in response.text


def test_shell_and_session_responses_apply_browser_security_policy() -> None:
    with TestClient(create_app(viewer=RecordingViewer())) as client:
        shell = client.get("/")
        session = client.get("/api/session")

    assert shell.headers["x-content-type-options"] == "nosniff"
    assert shell.headers["referrer-policy"] == "no-referrer"
    assert shell.headers["x-frame-options"] == "SAMEORIGIN"
    assert shell.headers["content-security-policy"] == (
        "default-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; "
        "form-action 'self'"
    )
    assert session.headers["cache-control"] == "no-store"


def test_schematic_workspace_preserves_encoded_path_and_repeated_query() -> None:
    viewer = RecordingViewer()
    with TestClient(create_app(viewer=viewer)) as client:
        client.get("/schematics/api/items/a%2Fb?tag=one&tag=two%20words")

    assert viewer.requests[0].path == "/api/items/a%2Fb"
    assert viewer.requests[0].query == b"tag=one&tag=two%20words"


def test_malformed_admin_cookie_fails_closed_as_anonymous(tmp_path: Path) -> None:
    with TestClient(
        create_app(
            viewer=RecordingViewer(),
            settings=_admin_settings(tmp_path, allow_insecure_admin=True),
        )
    ) as client:
        client.cookies.set("lantern_minecraft_admin", "not.valid@@")
        response = client.get("/api/session")

    assert response.status_code == 200
    assert response.json() == {"enabled": True, "authenticated": False}


def test_viewer_timeout_keeps_ui_live_but_readiness_closed() -> None:
    with TestClient(create_app(viewer=TimedOutViewer())) as client:
        health = client.get("/healthz")
        readiness = client.get("/readyz")
        proxy = client.get("/schematics/")

    assert health.status_code == 200
    assert readiness.status_code == 503
    assert readiness.json()["status"] == "not-ready"
    assert proxy.status_code == 504
    assert proxy.json() == {"detail": "schematic conversion timed out"}


def test_malformed_content_length_is_rejected_before_upstream() -> None:
    viewer = RecordingViewer()
    with TestClient(create_app(viewer=viewer), raise_server_exceptions=False) as client:
        response = client.post(
            "/schematics/api/convert",
            headers={"Origin": "http://testserver", "Content-Length": "not-a-number"},
            content=b"body",
        )

    assert response.status_code == 400
    assert viewer.requests == []


def test_upstream_is_closed_when_streaming_response_fails() -> None:
    viewer = ClosingFailingViewer()
    with pytest.raises(RuntimeError, match="upstream stream failed"):
        with TestClient(create_app(viewer=viewer)) as client:
            client.get("/schematics/")

    assert viewer.closed is True
