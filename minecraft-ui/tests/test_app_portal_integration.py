from collections.abc import AsyncIterator

from argon2 import PasswordHasher
from fastapi.testclient import TestClient

import app.main as main_module
from app.audit_log import InMemoryAuditLog
from app.catalog_workflow import (
    CatalogWorkflow,
    InMemoryCatalog,
    InMemorySubmissionStore,
    QueuePolicy,
)
from app.identity import (
    COOKIE_NAME,
    AdminUser,
    CookieMode,
    InMemoryIdentityDirectory,
    NamedAdminIdentity,
)
from app.main import create_app
from app.minecraft_control import InMemoryMinecraftControl
from app.portal import ConfirmationTokens, MinecraftPortal, NamedViewerAccess
from app.schematic_workspace import ProxyRequest, ProxyResponse


class Viewer:
    async def exchange(self, request: ProxyRequest) -> ProxyResponse:
        async def body() -> AsyncIterator[bytes]:
            yield b"{}"

        return ProxyResponse(200, [("content-type", "application/json")], body())

    async def aclose(self) -> None:
        return None


class Runtime:
    def __init__(self, *, queue_policy: QueuePolicy | None = None) -> None:
        identity = NamedAdminIdentity(
            InMemoryIdentityDirectory(
                [
                    AdminUser(
                        "iveri",
                        PasswordHasher().hash("portal password"),
                        "admin",
                        "iveri",
                        1,
                    )
                ]
            ),
            session_secret=b"s" * 32,
            ttl_seconds=3600,
            cookie_mode=CookieMode.TRUSTED_LAN,
        )
        catalog = CatalogWorkflow(
            InMemorySubmissionStore(), InMemoryCatalog(), queue_policy=queue_policy
        )
        self.portal = MinecraftPortal(
            identity=identity,
            control=InMemoryMinecraftControl(),
            catalog=catalog,
            audit=InMemoryAuditLog(),
            confirmations=ConfirmationTokens(b"c" * 32),
        )
        self.viewer_access = NamedViewerAccess(identity, "v" * 32)

    async def aclose(self) -> None:
        return None


def test_named_portal_routes_power_and_guest_schematic_submission() -> None:
    with TestClient(create_app(viewer=Viewer(), runtime=Runtime())) as client:
        workspace = client.get("/api/workspace")
        power = client.post(
            "/api/intents",
            headers={"Origin": "http://testserver"},
            json={
                "type": "minecraft.power",
                "action": "start",
                "idempotencyKey": "browser-request-1",
            },
        )
        submission = client.post(
            "/api/submissions",
            headers={
                "Origin": "http://testserver",
                "X-Schematic-Filename": "starter-factory.nbt",
                "X-Schematic-Promote": "true",
            },
            content=b"synthetic nbt fixture",
        )

    assert workspace.status_code == 200
    assert workspace.json()["session"]["role"] == "guest"
    assert power.json()["outcome"] == "done"
    assert submission.json()["submission"]["state"] == "cataloged"


def test_named_login_replaces_the_legacy_shared_password_route() -> None:
    with TestClient(create_app(viewer=Viewer(), runtime=Runtime())) as client:
        legacy = client.post(
            "/api/session/login",
            headers={"Origin": "http://testserver"},
            json={"password": "portal password"},
        )
        named = client.post(
            "/api/intents",
            headers={"Origin": "http://testserver"},
            json={
                "type": "session.login",
                "username": "iveri",
                "password": "portal password",
            },
        )
        workspace = client.get("/api/workspace")

    assert legacy.status_code == 410
    assert named.status_code == 200
    assert workspace.json()["session"]["actor"] == "iveri"


def test_named_admin_can_download_queued_payload_with_safe_response_headers() -> None:
    with TestClient(create_app(viewer=Viewer(), runtime=Runtime())) as client:
        upload = client.post(
            "/api/submissions",
            headers={
                "Origin": "http://testserver",
                "X-Schematic-Filename": "review schematic.nbt",
            },
            content=b"review payload",
        )
        submission_id = upload.json()["submission"]["id"]
        url = f"/api/admin/submissions/{submission_id}/download"
        assert client.get(url).status_code == 401
        client.post(
            "/api/intents",
            headers={"Origin": "http://testserver"},
            json={
                "type": "session.login",
                "username": "iveri",
                "password": "portal password",
            },
        )
        downloaded = client.get(url)

    assert downloaded.content == b"review payload"
    assert downloaded.headers["cache-control"] == "no-store"
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    assert "review%20schematic.nbt" in downloaded.headers["content-disposition"]


def test_upload_authorization_happens_before_body_buffering(monkeypatch) -> None:
    async def must_not_buffer(*_args, **_kwargs):
        raise AssertionError("unauthorized body was buffered")

    monkeypatch.setattr(main_module, "_read_binary_body", must_not_buffer)
    with TestClient(create_app(viewer=Viewer(), runtime=Runtime())) as client:
        schematic = client.post(
            "/api/submissions",
            headers={
                "Origin": "http://attacker.invalid",
                "X-Schematic-Filename": "blocked.nbt",
            },
            content=b"must not be read",
        )
        mod = client.post(
            "/api/admin/mods",
            headers={
                "Origin": "http://testserver",
                "X-Mod-Filename": "blocked.jar",
                "Idempotency-Key": "blocked-request-123",
            },
            content=b"must not be read",
        )

    assert schematic.status_code == 403
    assert mod.status_code == 401


def test_upload_capacity_is_claimed_before_body_and_released_after_read_failure(
    monkeypatch,
) -> None:
    runtime = Runtime(queue_policy=QueuePolicy(max_concurrent_uploads=1))
    held = runtime.portal.catalog.begin_upload(source_key="held-peer", reserved_bytes=1)

    async def must_not_buffer(*_args, **_kwargs):
        raise AssertionError("capacity denial reached body buffering")

    monkeypatch.setattr(main_module, "_read_binary_body", must_not_buffer)
    with TestClient(create_app(viewer=Viewer(), runtime=runtime)) as client:
        denied = client.post(
            "/api/submissions",
            headers={
                "Origin": "http://testserver",
                "X-Schematic-Filename": "blocked.nbt",
            },
            content=b"must not be read",
        )
    assert denied.status_code == 507

    runtime.portal.catalog.cancel_upload(held.admission_id)

    async def fail_while_reading(*_args, **_kwargs):
        from fastapi import HTTPException

        raise HTTPException(413, "synthetic stream failure")

    monkeypatch.setattr(main_module, "_read_binary_body", fail_while_reading)
    with TestClient(create_app(viewer=Viewer(), runtime=runtime)) as client:
        failed = client.post(
            "/api/submissions",
            headers={
                "Origin": "http://testserver",
                "X-Schematic-Filename": "failed.nbt",
            },
            content=b"partial",
        )
    assert failed.status_code == 413
    released = runtime.portal.catalog.begin_upload(source_key="after-failure", reserved_bytes=1)
    runtime.portal.catalog.cancel_upload(released.admission_id)


def test_logout_revokes_the_captured_session_token_server_side() -> None:
    with TestClient(create_app(viewer=Viewer(), runtime=Runtime())) as client:
        client.post(
            "/api/intents",
            headers={"Origin": "http://testserver"},
            json={
                "type": "session.login",
                "username": "iveri",
                "password": "portal password",
            },
        )
        captured = client.cookies.get(COOKIE_NAME)
        logout = client.post(
            "/api/intents",
            headers={"Origin": "http://testserver"},
            json={"type": "session.logout"},
        )
        client.cookies.set(COOKIE_NAME, captured)
        workspace = client.get("/api/workspace")

    assert logout.status_code == 200
    assert workspace.json()["session"]["role"] == "guest"


def test_compatibility_logout_route_also_revokes_named_session() -> None:
    with TestClient(create_app(viewer=Viewer(), runtime=Runtime())) as client:
        client.post(
            "/api/intents",
            headers={"Origin": "http://testserver"},
            json={
                "type": "session.login",
                "username": "iveri",
                "password": "portal password",
            },
        )
        captured = client.cookies.get(COOKIE_NAME)
        logout = client.post("/api/session/logout", headers={"Origin": "http://testserver"})
        client.cookies.set(COOKIE_NAME, captured)
        workspace = client.get("/api/workspace")

    assert logout.status_code == 204
    assert workspace.json()["session"]["role"] == "guest"
