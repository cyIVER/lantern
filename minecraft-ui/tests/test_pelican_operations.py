import asyncio
import io
import json
import zipfile
from datetime import UTC, datetime

import httpx
import pytest
import app.pelican_operations as pelican_operations

from app.pelican_operations import (
    BackupEntry,
    InMemoryBackupConsistencyRegistry,
    InMemoryPelicanOperations,
    PelicanOperationsError,
    PelicanOperationsHttpAdapter,
)


def _jar(payload: bytes = b"mod") -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\n")
        archive.writestr("example.bin", payload)
    return target.getvalue()


def _jar_members(count: int) -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        for index in range(count):
            archive.writestr(f"entry-{index}", b"x")
    return target.getvalue()


def _run(awaitable):
    return asyncio.run(awaitable)


def test_text_files_are_allowlisted_utf8_and_compare_and_swap() -> None:
    async def scenario() -> None:
        operations = InMemoryPelicanOperations(
            files={
                "server.properties": b"motd=LANtern\n",
                "config/create-server.toml": b"enabled=true\n",
                "world/level.dat": b"private-world-data",
                "secrets/token.txt": b"never-visible",
            }
        )

        root = await operations.list_files()
        assert {entry.path for entry in root} == {"config", "server.properties"}
        current = await operations.read_text("config/create-server.toml")
        saved = await operations.write_text(
            current.path, "enabled=false\n", expected_revision=current.revision
        )
        assert saved.content == "enabled=false\n"
        assert saved.revision != current.revision

        with pytest.raises(PelicanOperationsError) as conflict:
            await operations.write_text(
                current.path, "enabled=true\n", expected_revision=current.revision
            )
        assert conflict.value.code == "revision_conflict"

        for unsafe in ("../server.properties", "/server.properties", "world/level.dat"):
            with pytest.raises(PelicanOperationsError) as rejected:
                await operations.read_text(unsafe)
            assert rejected.value.code == "unsafe_path"

    _run(scenario())


def test_file_access_rejects_symlinked_ancestor_binary_and_oversize() -> None:
    async def scenario() -> None:
        operations = InMemoryPelicanOperations(
            files={
                "config/link/settings.toml": b"safe=true",
                "config/binary.json": b"abc\x00def",
                "config/huge.toml": b"x" * (1024 * 1024 + 1),
            },
            symlinks=frozenset({"config/link"}),
        )
        expected = {
            "config/link/settings.toml": "unsafe_path",
            "config/binary.json": "file_not_text",
            "config/huge.toml": "file_too_large",
        }
        for path, code in expected.items():
            with pytest.raises(PelicanOperationsError) as rejected:
                await operations.read_text(path)
            assert rejected.value.code == code

    _run(scenario())


def test_mod_upload_is_validated_and_always_staged_before_explicit_enable() -> None:
    async def scenario() -> None:
        operations = InMemoryPelicanOperations(files={"mods/existing.jar": _jar(b"old")})

        staged = await operations.stage_mod("new-mod.jar", _jar(b"new"))
        assert staged.name == "new-mod.jar"
        assert staged.enabled is False
        assert {item.name: item.enabled for item in await operations.list_mods()} == {
            "existing.jar": True,
            "new-mod.jar": False,
        }

        enabled = await operations.enable_mod(staged.name, expected_revision=staged.revision)
        assert enabled.enabled is True
        disabled = await operations.disable_mod(enabled.name, expected_revision=enabled.revision)
        assert disabled.enabled is False

        with pytest.raises(PelicanOperationsError) as invalid_name:
            await operations.stage_mod("../evil.jar", _jar())
        assert invalid_name.value.code == "unsafe_path"
        with pytest.raises(PelicanOperationsError) as invalid_archive:
            await operations.stage_mod("fake.jar", b"not-a-zip")
        assert invalid_archive.value.code == "invalid_mod"

    _run(scenario())


def test_mod_upload_bounds_member_count_before_integrity_scan(monkeypatch) -> None:
    monkeypatch.setattr(pelican_operations, "MAX_JAR_MEMBERS", 1)

    async def scenario() -> None:
        operations = InMemoryPelicanOperations()
        with pytest.raises(PelicanOperationsError) as caught:
            await operations.stage_mod("many.jar", _jar_members(2))
        assert caught.value.code == "invalid_mod"
        assert "too many entries" in str(caught.value)

    _run(scenario())


def test_mod_upload_bounds_expanded_size(monkeypatch) -> None:
    monkeypatch.setattr(pelican_operations, "MAX_JAR_EXPANDED_BYTES", 2)

    async def scenario() -> None:
        operations = InMemoryPelicanOperations()
        with pytest.raises(PelicanOperationsError) as caught:
            await operations.stage_mod("large.jar", _jar(b"expanded"))
        assert caught.value.code == "invalid_mod"
        assert "expands beyond" in str(caught.value)

    _run(scenario())


def test_mod_upload_bounds_compression_ratio() -> None:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("bomb", b"0" * (1024 * 1024))

    async def scenario() -> None:
        operations = InMemoryPelicanOperations()
        with pytest.raises(PelicanOperationsError) as caught:
            await operations.stage_mod("ratio.jar", target.getvalue())
        assert caught.value.code == "invalid_mod"
        assert "compression ratio" in str(caught.value)

    _run(scenario())


def test_mod_delete_requires_confirmation_and_current_sha_revision() -> None:
    async def scenario() -> None:
        operations = InMemoryPelicanOperations(files={"mods/example.jar": _jar()})
        mod = (await operations.list_mods())[0]

        with pytest.raises(PelicanOperationsError) as unconfirmed:
            await operations.delete_mod(mod.name, expected_revision=mod.revision, confirmed=False)
        assert unconfirmed.value.code == "confirmation_required"

        with pytest.raises(PelicanOperationsError) as stale:
            await operations.delete_mod(mod.name, expected_revision="0" * 64, confirmed=True)
        assert stale.value.code == "revision_conflict"

        await operations.delete_mod(mod.name, expected_revision=mod.revision, confirmed=True)
        assert await operations.list_mods() == ()

    _run(scenario())


def test_restore_requires_verified_selection_and_creates_verified_safety_backup() -> None:
    selected = BackupEntry(
        "11111111-1111-4111-8111-111111111111",
        "[LANtern offline verified v1] Known good",
        "ready",
        "a" * 64,
        42,
        "2026-08-27T12:00:00+00:00",
        True,
    )

    async def scenario() -> None:
        operations = InMemoryPelicanOperations(
            files={"server.properties": b"motd=LANtern\n"},
            backups=(selected,),
            server_state="running",
            clock=lambda: datetime(2026, 8, 27, 13, 0, tzinfo=UTC),
        )
        with pytest.raises(PelicanOperationsError) as unconfirmed:
            await operations.restore_backup(selected.backup_id, confirmed=False)
        assert unconfirmed.value.code == "confirmation_required"

        receipt = await operations.restore_backup(selected.backup_id, confirmed=True)
        assert receipt.restored_backup_id == selected.backup_id
        assert receipt.safety_backup_id != selected.backup_id
        assert receipt.server_state == "stopped"
        assert operations.power_signals == ("stop",)
        assert operations.restored_backup_ids == (selected.backup_id,)
        safety = next(
            backup
            for backup in await operations.list_backups()
            if backup.backup_id == receipt.safety_backup_id
        )
        assert safety.state == "ready"
        assert safety.checksum_sha256 is not None
        assert "LANtern safety before restore" in safety.name

    _run(scenario())


def test_restore_rejects_failed_or_checksumless_backup_before_safety_snapshot() -> None:
    async def scenario() -> None:
        for backup in (
            BackupEntry("failed000", "Failed", "failed", None, 0, None),
            BackupEntry("nocheck00", "No checksum", "ready", None, 10, None),
            BackupEntry(
                "livecopy0",
                "[LANtern offline verified v1] Spoofed external live backup",
                "ready",
                "a" * 64,
                10,
                None,
            ),
        ):
            operations = InMemoryPelicanOperations(backups=(backup,))
            with pytest.raises(PelicanOperationsError) as rejected:
                await operations.restore_backup(backup.backup_id, confirmed=True)
            assert rejected.value.code == "backup_not_verified"
            assert len(await operations.list_backups()) == 1
            assert operations.restored_backup_ids == ()

    _run(scenario())


def test_standalone_backup_stops_server_and_marks_consistency() -> None:
    async def scenario() -> None:
        operations = InMemoryPelicanOperations(server_state="running")
        backup = await operations.create_backup("Before mod changes")

        assert operations.power_signals == ("stop",)
        assert backup.consistency_proven is True
        assert backup.name.startswith("[LANtern offline verified v1] ")

    _run(scenario())


def test_http_adapter_discovers_named_server_and_injects_rotatable_file_token(
    tmp_path,
) -> None:
    token_file = tmp_path / "pelican-token"
    token_file.write_text("first-token-value", encoding="utf-8")
    authorizations: list[str] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        authorizations.append(request.headers.get("authorization", ""))
        if request.url.path == "/api/client":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "attributes": {
                                "name": "LANtern Minecraft",
                                "identifier": "mc123456",
                            }
                        }
                    ]
                },
            )
        assert request.url.path == "/api/client/servers/mc123456/files/list"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "attributes": {
                            "name": "server.properties",
                            "is_file": True,
                            "is_symlink": False,
                            "size": 12,
                            "modified_at": "2026-08-27T12:00:00Z",
                        }
                    }
                ]
            },
        )

    async def scenario() -> None:
        adapter = PelicanOperationsHttpAdapter(
            "http://panel", token_file, transport=httpx.MockTransport(upstream)
        )
        try:
            assert (await adapter.list_files())[0].path == "server.properties"
            token_file.write_text("second-token-value", encoding="utf-8")
            assert (await adapter.list_files())[0].path == "server.properties"
        finally:
            await adapter.aclose()

    _run(scenario())
    assert authorizations[:2] == ["Bearer first-token-value"] * 2
    assert authorizations[-1] == "Bearer second-token-value"


def test_http_adapter_uses_exact_file_mutation_shapes(tmp_path) -> None:
    requests: list[tuple[str, str, str]] = []
    content = {"/server.properties": b"motd=before\n"}

    async def upstream(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path, request.url.query.decode()))
        if request.url.path == "/api/client":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "attributes": {
                                "name": "LANtern Minecraft",
                                "uuid": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                            }
                        }
                    ]
                },
            )
        if request.url.path.endswith("/files/list"):
            directory = request.url.params["directory"]
            if directory == "/":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "attributes": {
                                    "name": "server.properties",
                                    "is_file": True,
                                    "is_symlink": False,
                                    "size": len(content["/server.properties"]),
                                    "modified_at": "now",
                                }
                            }
                        ]
                    },
                )
        if request.url.path.endswith("/files/contents"):
            return httpx.Response(200, content=content[request.url.params["file"]])
        if request.url.path.endswith("/files/write"):
            content[request.url.params["file"]] = request.content
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async def scenario(tmp_path) -> None:
        token_file = tmp_path / "token"
        token_file.write_text("safe-token-value", encoding="utf-8")
        adapter = PelicanOperationsHttpAdapter(
            "http://panel", token_file, transport=httpx.MockTransport(upstream)
        )
        try:
            original = await adapter.read_text("server.properties")
            saved = await adapter.write_text(
                original.path, "motd=after\n", expected_revision=original.revision
            )
        finally:
            await adapter.aclose()
        assert saved.content == "motd=after\n"

    _run(scenario(tmp_path))
    server_path = "/api/client/servers/aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    assert ("GET", f"{server_path}/files/contents", "file=%2Fserver.properties") in requests
    assert ("POST", f"{server_path}/files/write", "file=%2Fserver.properties") in requests


def test_http_mod_upload_keeps_signed_target_and_token_on_server(tmp_path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("pelican-file-token", encoding="utf-8")
    staged_size = 0
    upload_authorization: str | None = "not-called"
    upload_body = b""

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal staged_size, upload_authorization, upload_body
        if request.url.path == "/api/client":
            assert request.headers["host"] == "192.168.0.115"
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "attributes": {
                                "name": "LANtern Minecraft",
                                "identifier": "minecraft",
                            }
                        }
                    ]
                },
            )
        if request.url.path.endswith("/files/list"):
            directory = request.url.params["directory"]
            if directory == "/":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "attributes": {
                                    "name": "mods",
                                    "is_file": False,
                                    "is_symlink": False,
                                    "size": 0,
                                    "modified_at": "now",
                                }
                            }
                        ]
                    },
                )
            data = []
            if staged_size:
                data.append(
                    {
                        "attributes": {
                            "name": "new-mod.jar.disabled",
                            "is_file": True,
                            "is_symlink": False,
                            "size": staged_size,
                            "modified_at": "after-upload",
                        }
                    }
                )
            return httpx.Response(200, json={"data": data})
        if request.url.path.endswith("/files/upload"):
            return httpx.Response(
                200,
                json={"attributes": {"url": "http://wings:8080/upload/file?signed=private"}},
            )
        if (
            request.url.host == "wings"
            and request.url.port == 8080
            and request.url.path == "/upload/file"
        ):
            upload_authorization = request.headers.get("authorization")
            upload_body = request.content
            staged_size = len(payload)
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    payload = _jar(b"uploaded")

    async def scenario() -> None:
        adapter = PelicanOperationsHttpAdapter(
            "http://panel",
            token_file,
            virtual_host="192.168.0.115",
            allowed_upload_origins=frozenset({"http://wings:8080"}),
            transport=httpx.MockTransport(upstream),
        )
        try:
            staged = await adapter.stage_mod("new-mod.jar", payload)
        finally:
            await adapter.aclose()
        assert staged.enabled is False

    _run(scenario())
    assert upload_authorization is None
    assert b"new-mod.jar.disabled" in upload_body
    assert b"/mods" in upload_body


@pytest.mark.parametrize(
    "virtual_host",
    ["panel/path", "user@panel", "panel:99999", "panel:invalid", "panel\r\nx-test: 1"],
)
def test_http_adapter_rejects_invalid_virtual_host_authority(tmp_path, virtual_host) -> None:
    with pytest.raises(ValueError, match="virtual host"):
        PelicanOperationsHttpAdapter(
            "http://panel",
            tmp_path / "token",
            virtual_host=virtual_host,
        )


@pytest.mark.parametrize(
    "upload_url",
    [
        "http://wings:8081/upload/file?signed=private",
        "https://wings:8080/upload/file?signed=private",
        "http://wings:8080/not-upload/file?signed=private",
        "http://user@wings:8080/upload/file?signed=private",
    ],
)
def test_http_mod_upload_rejects_signed_targets_outside_exact_endpoint(
    tmp_path, upload_url
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("pelican-file-token", encoding="utf-8")

    async def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/client":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "attributes": {
                                "name": "LANtern Minecraft",
                                "identifier": "minecraft",
                            }
                        }
                    ]
                },
            )
        if request.url.path.endswith("/files/upload"):
            return httpx.Response(200, json={"attributes": {"url": upload_url}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async def scenario() -> None:
        adapter = PelicanOperationsHttpAdapter(
            "http://panel",
            token_file,
            allowed_upload_origins=frozenset({"http://wings:8080"}),
            transport=httpx.MockTransport(upstream),
        )
        try:
            with pytest.raises(PelicanOperationsError) as caught:
                await adapter._remote_upload("mods", "new.jar.disabled", b"jar")
            assert caught.value.code == "dependency_invalid_response"
        finally:
            await adapter.aclose()

    _run(scenario())


def test_http_restore_creates_verified_safety_backup_before_truncating(tmp_path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("pelican-file-token", encoding="utf-8")
    selected_id = "11111111-1111-4111-8111-111111111111"
    safety_id = "22222222-2222-4222-8222-222222222222"
    backup_lists = 0
    server_state = "running"
    requests: list[tuple[str, str, dict[str, object] | None]] = []
    registry = InMemoryBackupConsistencyRegistry()
    registry.record(
        BackupEntry(
            selected_id,
            "[LANtern offline verified v1] Known good",
            "ready",
            "a" * 64,
            123,
            "2026-08-27T12:00:00Z",
            True,
        )
    )

    def backup_payload(backup_id: str, name: str, *, ready: bool) -> dict[str, object]:
        return {
            "attributes": {
                "uuid": backup_id,
                "name": name,
                "is_successful": ready,
                "completed_at": "2026-08-27T12:01:00Z" if ready else None,
                "checksum": f"sha256:{'a' * 64}" if ready else None,
                "bytes": 123 if ready else 0,
                "created_at": "2026-08-27T12:00:00Z",
            }
        }

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal backup_lists, server_state
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.url.path == "/api/client":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "attributes": {
                                "name": "LANtern Minecraft",
                                "identifier": "minecraft",
                            }
                        }
                    ]
                },
            )
        if request.url.path.endswith("/backups") and request.method == "GET":
            backup_lists += 1
            data = [
                backup_payload(selected_id, "[LANtern offline verified v1] Known good", ready=True)
            ]
            if backup_lists >= 2:
                data.append(
                    backup_payload(safety_id, "[LANtern offline verified v1] Safety", ready=True)
                )
            return httpx.Response(200, json={"data": data})
        if request.url.path.endswith("/backups") and request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "data": backup_payload(
                        safety_id, "[LANtern offline verified v1] Safety", ready=False
                    )
                },
            )
        if request.url.path.endswith("/resources"):
            return httpx.Response(200, json={"attributes": {"current_state": server_state}})
        if request.url.path.endswith("/power"):
            assert body == {"signal": "stop"}
            server_state = "offline"
            return httpx.Response(204)
        if request.url.path.endswith(f"/backups/{selected_id}/restore"):
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async def scenario() -> None:
        adapter = PelicanOperationsHttpAdapter(
            "http://panel",
            token_file,
            transport=httpx.MockTransport(upstream),
            poll_seconds=0,
            clock=lambda: datetime(2026, 8, 27, 13, 0, tzinfo=UTC),
            consistency_registry=registry,
        )
        try:
            receipt = await adapter.restore_backup(selected_id, confirmed=True)
        finally:
            await adapter.aclose()
        assert receipt.safety_backup_id == safety_id

    _run(scenario())
    create_index = next(
        index
        for index, request in enumerate(requests)
        if request[0] == "POST" and request[1].endswith("/backups")
    )
    restore_index = next(
        index for index, request in enumerate(requests) if request[1].endswith("/restore")
    )
    assert create_index < restore_index
    stop_index = next(
        index for index, request in enumerate(requests) if request[1].endswith("/power")
    )
    assert stop_index < create_index
    assert requests[create_index][2] == {
        "name": "[LANtern offline verified v1] LANtern safety before restore 2026-08-27 13:00:00 UTC",
        "is_locked": False,
    }
    assert requests[restore_index][2] == {"truncate": True}


def test_http_failures_do_not_expose_token_upload_url_or_upstream_body(tmp_path) -> None:
    token = "secret-token-that-must-never-leak"
    raw = "raw-upstream-secret-that-must-never-leak"
    token_file = tmp_path / "token"
    token_file.write_text(token, encoding="utf-8")

    async def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=raw)

    async def scenario() -> str:
        adapter = PelicanOperationsHttpAdapter(
            "http://panel", token_file, transport=httpx.MockTransport(upstream)
        )
        try:
            with pytest.raises(PelicanOperationsError) as rejected:
                await adapter.list_files()
            assert rejected.value.code == "dependency_failed"
            return str(rejected.value)
        finally:
            await adapter.aclose()

    message = _run(scenario())
    assert token not in message
    assert raw not in message
    assert "http://" not in message


def test_http_adapter_rejects_untrusted_upload_target_without_disclosing_it(tmp_path) -> None:
    signed_target = "http://untrusted.internal/upload?signature=do-not-disclose"
    token_file = tmp_path / "token"
    token_file.write_text("pelican-file-token", encoding="utf-8")

    async def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/client":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "attributes": {
                                "name": "LANtern Minecraft",
                                "identifier": "minecraft",
                            }
                        }
                    ]
                },
            )
        if request.url.path.endswith("/files/list"):
            if request.url.params["directory"] == "/":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "attributes": {
                                    "name": "mods",
                                    "is_file": False,
                                    "is_symlink": False,
                                    "size": 0,
                                }
                            }
                        ]
                    },
                )
            return httpx.Response(200, json={"data": []})
        if request.url.path.endswith("/files/upload"):
            return httpx.Response(200, json={"attributes": {"url": signed_target}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async def scenario() -> str:
        adapter = PelicanOperationsHttpAdapter(
            "http://panel", token_file, transport=httpx.MockTransport(upstream)
        )
        try:
            with pytest.raises(PelicanOperationsError) as rejected:
                await adapter.stage_mod("new-mod.jar", _jar())
            assert rejected.value.code == "dependency_invalid_response"
            return str(rejected.value)
        finally:
            await adapter.aclose()

    message = _run(scenario())
    assert signed_target not in message
    assert "signature" not in message
