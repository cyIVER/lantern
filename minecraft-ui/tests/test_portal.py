import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

from argon2 import PasswordHasher
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

from app.audit_log import InMemoryAuditLog
from app.catalog_workflow import CatalogWorkflow, InMemoryCatalog, InMemorySubmissionStore
from app.identity import AdminUser, CookieMode, InMemoryIdentityDirectory, NamedAdminIdentity
from app.minecraft_control import InMemoryMinecraftControl
from app.pelican_operations import BackupEntry, InMemoryPelicanOperations
from app.portal import ConfirmationTokens, MinecraftPortal


def _portal(*, conflict: str | None = None, operations=None) -> MinecraftPortal:
    identity = NamedAdminIdentity(
        InMemoryIdentityDirectory(
            [
                AdminUser(
                    username="iveri",
                    password_hash=PasswordHasher().hash("correct horse battery staple"),
                    role="admin",
                    upstream_alias="iveri",
                    credential_version=1,
                )
            ]
        ),
        session_secret=b"s" * 32,
        ttl_seconds=3600,
        cookie_mode=CookieMode.TRUSTED_LAN,
    )
    return MinecraftPortal(
        identity=identity,
        control=InMemoryMinecraftControl(conflicting_game=conflict),
        catalog=CatalogWorkflow(InMemorySubmissionStore(), InMemoryCatalog()),
        audit=InMemoryAuditLog(),
        confirmations=ConfirmationTokens(b"c" * 32),
        operations=operations,
    )


def _app(portal: MinecraftPortal) -> FastAPI:
    app = FastAPI()

    @app.get("/api/workspace")
    async def workspace(request: Request):
        return await portal.workspace(request)

    @app.post("/api/intents")
    async def intent(request: Request, response: Response):
        return await portal.execute(request, response, await request.json())

    @app.post("/api/submissions")
    async def submission(request: Request):
        return await portal.submit_schematic(
            request,
            filename=request.headers["x-schematic-filename"],
            content=await request.body(),
            promote=request.headers.get("x-schematic-promote") == "true",
            metadata={},
        )

    @app.get("/api/admin/submissions/{submission_id}/download")
    async def download(request: Request, submission_id: str):
        _filename, content = await portal.download_submission(request, submission_id)
        return Response(content, media_type="application/octet-stream")

    return app


def test_workspace_gives_guests_power_affordances_but_no_admin_projection() -> None:
    with TestClient(_app(_portal())) as client:
        workspace = client.get("/api/workspace").json()

    assert workspace["session"] == {
        "enabled": True,
        "authenticated": False,
        "actor": None,
        "role": "guest",
    }
    assert workspace["minecraft"]["allowed"] == ["restart", "start", "stop"]
    assert workspace["admin"] is None


def test_named_login_returns_admin_projection() -> None:
    with TestClient(_app(_portal())) as client:
        login = client.post(
            "/api/intents",
            headers={"Origin": "http://testserver"},
            json={
                "type": "session.login",
                "username": "iveri",
                "password": "correct horse battery staple",
            },
        )
        workspace = client.get("/api/workspace").json()

    assert login.status_code == 200
    assert workspace["session"]["actor"] == "iveri"
    assert workspace["session"]["role"] == "admin"
    assert workspace["admin"] is not None


def test_power_conflict_requires_peer_bound_confirmation() -> None:
    portal = _portal(conflict="Stardew Valley")
    with TestClient(_app(portal)) as client:
        first = client.post(
            "/api/intents",
            headers={"Origin": "http://testserver"},
            json={
                "type": "minecraft.power",
                "action": "start",
                "idempotencyKey": "request-12345678",
            },
        )
        challenge = first.json()["challenge"]
        second = client.post(
            "/api/intents",
            headers={"Origin": "http://testserver"},
            json={
                "type": "minecraft.power",
                "action": "start",
                "idempotencyKey": "request-12345678",
                "confirmation": challenge["token"],
            },
        )
        replay = client.post(
            "/api/intents",
            headers={"Origin": "http://testserver"},
            json={
                "type": "minecraft.power",
                "action": "start",
                "idempotencyKey": "request-12345678",
                "confirmation": challenge["token"],
            },
        )

    assert first.json()["outcome"] == "confirmation_required"
    assert challenge["effects"] == ["Stop Stardew Valley"]
    assert second.json()["outcome"] == "done"
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "idempotency_replayed"


def test_all_intents_require_exact_origin() -> None:
    with TestClient(_app(_portal())) as client:
        response = client.post(
            "/api/intents",
            headers={"Origin": "http://other-host"},
            json={"type": "session.logout"},
        )

    assert response.status_code == 403


def test_admin_workspace_projects_safe_file_mod_and_restore_summaries() -> None:
    operations = InMemoryPelicanOperations(
        files={
            "server.properties": b"motd=LANtern\n",
            "mods/example.jar.disabled": b"not inspected while listing",
        },
        backups=(
            BackupEntry(
                backup_id="backup-1",
                name="Verified backup",
                state="ready",
                checksum_sha256="a" * 64,
                byte_size=1024,
                created_at="2026-08-27T12:00:00Z",
            ),
        ),
    )
    with TestClient(_app(_portal(operations=operations))) as client:
        client.post(
            "/api/intents",
            headers={"Origin": "http://testserver"},
            json={
                "type": "session.login",
                "username": "iveri",
                "password": "correct horse battery staple",
            },
        )
        workspace = client.get("/api/workspace").json()

    assert workspace["admin"]["files"]["entries"][0]["path"] == "server.properties"
    assert workspace["admin"]["mods"]["entries"][0]["name"] == "example.jar"
    assert workspace["admin"]["restores"][0]["backup_id"] == "backup-1"


def test_backup_creation_discloses_stop_and_leaves_server_offline() -> None:
    operations = InMemoryPelicanOperations(server_state="running")
    with TestClient(_app(_portal(operations=operations))) as client:
        client.post(
            "/api/intents",
            headers={"Origin": "http://testserver"},
            json={
                "type": "session.login",
                "username": "iveri",
                "password": "correct horse battery staple",
            },
        )
        intent = {
            "type": "backup.create",
            "name": "Before updates",
            "idempotencyKey": "backup-request-123",
        }
        challenge = client.post(
            "/api/intents", headers={"Origin": "http://testserver"}, json=intent
        ).json()
        result = client.post(
            "/api/intents",
            headers={"Origin": "http://testserver"},
            json={**intent, "confirmation": challenge["challenge"]["token"]},
        ).json()

    assert "remain stopped" in challenge["challenge"]["effects"][-1]
    assert result["backup"]["consistency_proven"] is True
    assert operations.power_signals == ("stop",)


def test_guest_power_cannot_race_offline_backup_critical_section() -> None:
    entered = threading.Event()
    release = threading.Event()
    power_called = threading.Event()

    class BlockingOperations(InMemoryPelicanOperations):
        async def create_backup(self, name):
            entered.set()
            await asyncio.to_thread(release.wait)
            return await super().create_backup(name)

    class TrackingControl(InMemoryMinecraftControl):
        async def power(self, action, *, confirmed):
            power_called.set()
            return await super().power(action, confirmed=confirmed)

    portal = _portal(operations=BlockingOperations(server_state="running"))
    portal.control = TrackingControl()
    with TestClient(_app(portal)) as client:
        client.post(
            "/api/intents",
            headers={"Origin": "http://testserver"},
            json={
                "type": "session.login",
                "username": "iveri",
                "password": "correct horse battery staple",
            },
        )
        backup_intent = {
            "type": "backup.create",
            "name": "Interlock proof",
            "idempotencyKey": "backup-interlock-1",
        }
        challenge = client.post(
            "/api/intents", headers={"Origin": "http://testserver"}, json=backup_intent
        ).json()["challenge"]["token"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            backup_future = pool.submit(
                client.post,
                "/api/intents",
                headers={"Origin": "http://testserver"},
                json={**backup_intent, "confirmation": challenge},
            )
            assert entered.wait(2)
            power_future = pool.submit(
                client.post,
                "/api/intents",
                headers={"Origin": "http://testserver"},
                json={
                    "type": "minecraft.power",
                    "action": "start",
                    "idempotencyKey": "power-interlock-1",
                },
            )
            assert power_called.wait(0.1) is False
            release.set()
            assert backup_future.result(timeout=2).status_code == 200
            assert power_future.result(timeout=2).status_code == 200
    assert power_called.is_set()


def test_guest_submission_autofills_and_promotes_when_requirements_pass() -> None:
    with TestClient(_app(_portal())) as client:
        response = client.post(
            "/api/submissions",
            headers={
                "Origin": "http://testserver",
                "X-Schematic-Filename": "brass-factory.nbt",
                "X-Schematic-Promote": "true",
            },
            content=b"synthetic canonical content",
        )

    assert response.status_code == 200
    assert response.json()["submission"]["state"] == "cataloged"
    assert response.json()["submission"]["title"] == "Brass Factory"


def test_admin_queue_projection_exposes_review_evidence_and_protected_payload() -> None:
    with TestClient(_app(_portal())) as client:
        upload = client.post(
            "/api/submissions",
            headers={
                "Origin": "http://testserver",
                "X-Schematic-Filename": "review-me.nbt",
            },
            content=b"schematic evidence",
        )
        submission_id = upload.json()["submission"]["id"]
        assert client.get(f"/api/admin/submissions/{submission_id}/download").status_code == 401
        client.post(
            "/api/intents",
            headers={"Origin": "http://testserver"},
            json={
                "type": "session.login",
                "username": "iveri",
                "password": "correct horse battery staple",
            },
        )
        workspace = client.get("/api/workspace").json()
        downloaded = client.get(f"/api/admin/submissions/{submission_id}/download")

    [queued] = workspace["admin"]["attention"]
    assert queued["metadata"]["filename"] == "review-me.nbt"
    assert queued["byteSize"] == len(b"schematic evidence")
    assert queued["requirements"]
    assert queued["recommendations"]
    assert queued["downloadUrl"].endswith(f"/{submission_id}/download")
    assert downloaded.content == b"schematic evidence"


def test_admin_file_mutation_claims_idempotency_key_before_write() -> None:
    operations = InMemoryPelicanOperations(files={"server.properties": b"motd=before\n"})
    with TestClient(_app(_portal(operations=operations))) as client:
        client.post(
            "/api/intents",
            headers={"Origin": "http://testserver"},
            json={
                "type": "session.login",
                "username": "iveri",
                "password": "correct horse battery staple",
            },
        )
        read = client.post(
            "/api/intents",
            headers={"Origin": "http://testserver"},
            json={
                "type": "file.read",
                "path": "server.properties",
                "idempotencyKey": "read-request-1234",
            },
        ).json()["document"]
        intent = {
            "type": "file.save",
            "path": "server.properties",
            "content": "motd=after\n",
            "expectedRevision": read["revision"],
            "idempotencyKey": "write-request-1234",
        }
        first = client.post("/api/intents", headers={"Origin": "http://testserver"}, json=intent)
        replay = client.post("/api/intents", headers={"Origin": "http://testserver"}, json=intent)

    assert first.status_code == 200
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "idempotency_replayed"
