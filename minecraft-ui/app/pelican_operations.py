"""Safe native Minecraft administration through Pelican's client interface.

The module deliberately exposes Minecraft-shaped operations rather than
Pelican-shaped requests.  Both the production HTTP adapter and the in-memory
adapter pass through the same validation and sequencing implementation, so a
caller cannot accidentally skip path policy, optimistic revisions, staged mod
activation, or the pre-restore safety backup.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import re
import sqlite3
import threading
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx

MAX_TEXT_BYTES = 1024 * 1024
MAX_MOD_BYTES = 256 * 1024 * 1024
MAX_JAR_MEMBERS = 100_000
MAX_JAR_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
MAX_JAR_ENTRY_COMPRESSION_RATIO = 1_000
MAX_JAR_TOTAL_COMPRESSION_RATIO = 500

_TEXT_SUFFIXES = frozenset(
    {".cfg", ".conf", ".json", ".json5", ".properties", ".toml", ".txt", ".yaml", ".yml"}
)
_CONFIG_FILES = frozenset(
    {
        "banned-ips.json",
        "banned-players.json",
        "ops.json",
        "server.properties",
        "whitelist.json",
    }
)
_CONFIG_ROOTS = ("config", "defaultconfigs", "world/serverconfig")
_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9 _+.,@()'\[\]-]+")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_OFFLINE_BACKUP_PREFIX = "[LANtern offline verified v1] "


@dataclass(frozen=True, slots=True)
class FileEntry:
    path: str
    name: str
    kind: Literal["file", "directory"]
    byte_size: int
    modified_at: str | None
    revision: str


@dataclass(frozen=True, slots=True)
class TextDocument:
    path: str
    content: str
    revision: str


@dataclass(frozen=True, slots=True)
class ModEntry:
    name: str
    enabled: bool
    byte_size: int
    modified_at: str | None
    revision: str


@dataclass(frozen=True, slots=True)
class BackupEntry:
    backup_id: str
    name: str
    state: Literal["pending", "ready", "failed"]
    checksum_sha256: str | None
    byte_size: int
    created_at: str | None
    consistency_proven: bool = False


@dataclass(frozen=True, slots=True)
class RestoreReceipt:
    restored_backup_id: str
    safety_backup_id: str
    server_state: Literal["stopped"] = "stopped"
    status: Literal["accepted"] = "accepted"


class BackupConsistencyRegistry(Protocol):
    def record(self, backup: BackupEntry) -> None: ...

    def contains(self, backup: BackupEntry) -> bool: ...


class InMemoryBackupConsistencyRegistry:
    def __init__(self) -> None:
        self._entries: set[tuple[str, str, str]] = set()

    def record(self, backup: BackupEntry) -> None:
        if backup.checksum_sha256:
            self._entries.add((backup.backup_id, backup.checksum_sha256, backup.name))

    def contains(self, backup: BackupEntry) -> bool:
        return bool(
            backup.checksum_sha256
            and (backup.backup_id, backup.checksum_sha256, backup.name) in self._entries
        )


class SqliteBackupConsistencyRegistry:
    """Durably bind an offline proof to the exact Pelican id, checksum, and name."""

    def __init__(self, database: str) -> None:
        self._connection = sqlite3.connect(database, check_same_thread=False)
        self._lock = threading.RLock()
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS minecraft_offline_backups (
                    backup_id TEXT PRIMARY KEY,
                    checksum_sha256 TEXT NOT NULL,
                    name TEXT NOT NULL
                )
                """
            )

    def record(self, backup: BackupEntry) -> None:
        if not backup.checksum_sha256:
            raise ValueError("a consistency proof requires a checksum")
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO minecraft_offline_backups VALUES (?, ?, ?)",
                (backup.backup_id, backup.checksum_sha256, backup.name),
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def contains(self, backup: BackupEntry) -> bool:
        if not backup.checksum_sha256:
            return False
        with self._lock:
            row = self._connection.execute(
                """
                SELECT 1 FROM minecraft_offline_backups
                WHERE backup_id = ? AND checksum_sha256 = ? AND name = ?
                """,
                (backup.backup_id, backup.checksum_sha256, backup.name),
            ).fetchone()
        return row is not None


class PelicanOperationsError(RuntimeError):
    """Stable, sanitized failure crossing the administration seam."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class MinecraftAdminOperations(Protocol):
    async def list_files(self, directory: str = "") -> tuple[FileEntry, ...]: ...

    async def read_text(self, path: str) -> TextDocument: ...

    async def write_text(
        self, path: str, content: str, *, expected_revision: str
    ) -> TextDocument: ...

    async def list_mods(self) -> tuple[ModEntry, ...]: ...

    async def stage_mod(self, filename: str, content: bytes) -> ModEntry: ...

    async def enable_mod(self, filename: str, *, expected_revision: str) -> ModEntry: ...

    async def disable_mod(self, filename: str, *, expected_revision: str) -> ModEntry: ...

    async def delete_mod(
        self, filename: str, *, expected_revision: str, confirmed: bool
    ) -> None: ...

    async def list_backups(self) -> tuple[BackupEntry, ...]: ...

    async def create_backup(self, name: str) -> BackupEntry: ...

    async def restore_backup(self, backup_id: str, *, confirmed: bool) -> RestoreReceipt: ...


@dataclass(frozen=True, slots=True)
class _RemoteEntry:
    name: str
    is_file: bool
    is_symlink: bool
    byte_size: int
    modified_at: str | None


class _SafeOperations:
    """Shared policy implementation; transport hooks form a private seam."""

    def __init__(
        self,
        *,
        backup_timeout_seconds: float = 300,
        poll_seconds: float = 2,
        clock: Callable[[], datetime] | None = None,
        consistency_registry: BackupConsistencyRegistry | None = None,
    ) -> None:
        self._backup_timeout_seconds = backup_timeout_seconds
        self._poll_seconds = poll_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._mutation_lock = asyncio.Lock()
        self._consistency_registry = consistency_registry or InMemoryBackupConsistencyRegistry()

    async def list_files(self, directory: str = "") -> tuple[FileEntry, ...]:
        clean = _config_directory(directory)
        if clean:
            await self._require_safe_chain(clean, expect_file=False)
        entries = await self._remote_list(clean)
        visible: list[FileEntry] = []
        for entry in entries:
            if entry.is_symlink or not _safe_segment(entry.name):
                continue
            path = f"{clean}/{entry.name}" if clean else entry.name
            if entry.is_file:
                try:
                    _config_file(path)
                except PelicanOperationsError:
                    continue
            elif not _visible_config_directory(path):
                continue
            visible.append(_file_entry(path, entry))
        return tuple(
            sorted(visible, key=lambda item: (item.kind != "directory", item.name.lower()))
        )

    async def read_text(self, path: str) -> TextDocument:
        clean = _config_file(path)
        entry = await self._require_safe_chain(clean, expect_file=True)
        if entry.byte_size > MAX_TEXT_BYTES:
            raise PelicanOperationsError("file_too_large", "Configuration file exceeds 1 MiB")
        content = await self._remote_read(clean)
        if len(content) > MAX_TEXT_BYTES:
            raise PelicanOperationsError("file_too_large", "Configuration file exceeds 1 MiB")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PelicanOperationsError(
                "file_not_text", "Configuration file is not valid UTF-8 text"
            ) from exc
        if "\x00" in text:
            raise PelicanOperationsError("file_not_text", "Configuration file is not text")
        return TextDocument(clean, text, _content_revision(content))

    async def write_text(self, path: str, content: str, *, expected_revision: str) -> TextDocument:
        clean = _config_file(path)
        _expected_revision(expected_revision)
        if "\x00" in content:
            raise PelicanOperationsError("file_not_text", "Configuration file is not text")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_TEXT_BYTES:
            raise PelicanOperationsError("file_too_large", "Configuration file exceeds 1 MiB")
        async with self._mutation_lock:
            current = await self.read_text(clean)
            if current.revision != expected_revision:
                raise PelicanOperationsError(
                    "revision_conflict", "Configuration changed; reload it before saving"
                )
            await self._remote_write(clean, encoded)
            saved = await self.read_text(clean)
            if saved.revision != _content_revision(encoded):
                raise PelicanOperationsError(
                    "write_verification_failed", "Pelican did not preserve the configuration update"
                )
            return saved

    async def list_mods(self) -> tuple[ModEntry, ...]:
        await self._require_safe_chain("mods", expect_file=False)
        mods = []
        for entry in await self._remote_list("mods"):
            if not entry.is_file or entry.is_symlink:
                continue
            try:
                logical, enabled = _stored_mod_name(entry.name)
            except PelicanOperationsError:
                continue
            mods.append(_mod_entry(logical, enabled, entry))
        return tuple(sorted(mods, key=lambda item: (item.name.lower(), not item.enabled)))

    async def stage_mod(self, filename: str, content: bytes) -> ModEntry:
        logical = _mod_name(filename)
        payload = bytes(content)
        if not payload:
            raise PelicanOperationsError("invalid_mod", "Mod JAR must not be empty")
        if len(payload) > MAX_MOD_BYTES:
            raise PelicanOperationsError("mod_too_large", "Mod JAR exceeds 256 MiB")
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                _validate_jar_expansion(archive)
                bad_member = archive.testzip()
        except (zipfile.BadZipFile, OSError) as exc:
            raise PelicanOperationsError("invalid_mod", "Uploaded mod is not a valid JAR") from exc
        if bad_member is not None:
            raise PelicanOperationsError("invalid_mod", "Uploaded mod JAR failed integrity checks")
        async with self._mutation_lock:
            await self._require_safe_chain("mods", expect_file=False)
            entries = {entry.name: entry for entry in await self._remote_list("mods")}
            if logical in entries or f"{logical}.disabled" in entries:
                raise PelicanOperationsError("mod_exists", "A mod with that name already exists")
            stored = f"{logical}.disabled"
            await self._remote_upload("mods", stored, payload)
            entry = await self._find_entry(f"mods/{stored}", expect_file=True)
            if entry.byte_size != len(payload):
                raise PelicanOperationsError(
                    "upload_verification_failed", "Pelican did not preserve the staged mod"
                )
            return _mod_entry(logical, False, entry)

    async def enable_mod(self, filename: str, *, expected_revision: str) -> ModEntry:
        return await self._set_mod_enabled(
            filename, enabled=True, expected_revision=expected_revision
        )

    async def disable_mod(self, filename: str, *, expected_revision: str) -> ModEntry:
        return await self._set_mod_enabled(
            filename, enabled=False, expected_revision=expected_revision
        )

    async def _set_mod_enabled(
        self, filename: str, *, enabled: bool, expected_revision: str
    ) -> ModEntry:
        logical = _mod_name(filename)
        _expected_revision(expected_revision)
        source_name = f"{logical}.disabled" if enabled else logical
        target_name = logical if enabled else f"{logical}.disabled"
        async with self._mutation_lock:
            source = await self._find_entry(f"mods/{source_name}", expect_file=True)
            if _entry_revision(source_name, source) != expected_revision:
                raise PelicanOperationsError(
                    "revision_conflict", "Mod changed; refresh before editing"
                )
            try:
                await self._find_entry(f"mods/{target_name}", expect_file=True)
            except PelicanOperationsError as exc:
                if exc.code != "not_found":
                    raise
            else:
                raise PelicanOperationsError("mod_exists", "The target mod state already exists")
            await self._remote_rename(f"mods/{source_name}", f"mods/{target_name}")
            changed = await self._find_entry(f"mods/{target_name}", expect_file=True)
            return _mod_entry(logical, enabled, changed)

    async def delete_mod(self, filename: str, *, expected_revision: str, confirmed: bool) -> None:
        if not confirmed:
            raise PelicanOperationsError(
                "confirmation_required", "Deleting a mod requires confirmation"
            )
        logical = _mod_name(filename)
        _expected_revision(expected_revision)
        async with self._mutation_lock:
            active = await self._optional_entry(f"mods/{logical}")
            staged = await self._optional_entry(f"mods/{logical}.disabled")
            candidates = [(logical, active), (f"{logical}.disabled", staged)]
            present = [(name, entry) for name, entry in candidates if entry is not None]
            if len(present) != 1:
                code = "not_found" if not present else "ambiguous_mod"
                message = (
                    "Mod was not found" if not present else "Both active and disabled mods exist"
                )
                raise PelicanOperationsError(code, message)
            stored, entry = present[0]
            if _entry_revision(stored, entry) != expected_revision:
                raise PelicanOperationsError(
                    "revision_conflict", "Mod changed; refresh before deleting"
                )
            await self._remote_delete("mods", (stored,))

    async def list_backups(self) -> tuple[BackupEntry, ...]:
        return tuple(sorted(await self._remote_backups(), key=_backup_sort_key, reverse=True))

    async def create_backup(self, name: str) -> BackupEntry:
        clean_name = _backup_name(name)
        async with self._mutation_lock:
            await self._ensure_server_stopped()
            return await self._create_backup_unlocked(clean_name)

    async def restore_backup(self, backup_id: str, *, confirmed: bool) -> RestoreReceipt:
        if not confirmed:
            raise PelicanOperationsError(
                "confirmation_required", "Restoring a backup requires confirmation"
            )
        clean_id = _backup_id(backup_id)
        async with self._mutation_lock:
            selected = await self._lookup_backup(clean_id)
            _require_verified_backup(selected, "Selected backup")
            await self._ensure_server_stopped()
            stamp = self._clock().astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
            safety = await self._create_backup_unlocked(f"LANtern safety before restore {stamp}")
            _require_verified_backup(safety, "Safety backup")
            # A backup can take minutes. Prove quiescence again immediately before
            # Pelican truncates live files; never restart implicitly after restore.
            await self._ensure_server_stopped()
            await self._remote_restore(selected.backup_id, truncate=True)
            return RestoreReceipt(selected.backup_id, safety.backup_id)

    async def _ensure_server_stopped(self) -> None:
        state = await self._remote_server_state()
        if state != "offline":
            await self._remote_power("stop")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._backup_timeout_seconds
        while await self._remote_server_state() != "offline":
            if loop.time() >= deadline:
                raise PelicanOperationsError(
                    "server_stop_timeout",
                    "Minecraft did not stop; no backup or restore was performed",
                )
            await asyncio.sleep(self._poll_seconds)

    async def _create_backup_unlocked(self, name: str) -> BackupEntry:
        marked_name = (
            name if name.startswith(_OFFLINE_BACKUP_PREFIX) else (f"{_OFFLINE_BACKUP_PREFIX}{name}")
        )
        backup_id = _backup_id(await self._remote_create_backup(marked_name))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._backup_timeout_seconds
        while True:
            backup = await self._lookup_backup(backup_id)
            if backup.state == "ready":
                _require_successful_checksum(backup, "Backup")
                self._consistency_registry.record(backup)
                backup = replace(backup, consistency_proven=True)
                _require_verified_backup(backup, "Backup")
                return backup
            if backup.state == "failed":
                raise PelicanOperationsError("backup_failed", "Pelican could not create the backup")
            if loop.time() >= deadline:
                raise PelicanOperationsError(
                    "dependency_timeout", "Pelican did not finish the backup in time"
                )
            await asyncio.sleep(self._poll_seconds)

    async def _lookup_backup(self, backup_id: str) -> BackupEntry:
        for backup in await self._remote_backups():
            if backup.backup_id == backup_id:
                return backup
        raise PelicanOperationsError("not_found", "Backup was not found")

    async def _optional_entry(self, path: str) -> _RemoteEntry | None:
        try:
            return await self._find_entry(path, expect_file=True)
        except PelicanOperationsError as exc:
            if exc.code == "not_found":
                return None
            raise

    async def _require_safe_chain(self, path: str, *, expect_file: bool) -> _RemoteEntry:
        parts = path.split("/")
        parent = ""
        found: _RemoteEntry | None = None
        for index, part in enumerate(parts):
            found = await self._find_child(parent, part)
            final = index == len(parts) - 1
            if found.is_symlink:
                raise PelicanOperationsError("unsafe_path", "Symbolic links are not permitted")
            if final and found.is_file != expect_file:
                raise PelicanOperationsError("wrong_file_type", "Path has the wrong file type")
            if not final and found.is_file:
                raise PelicanOperationsError("unsafe_path", "A parent path is not a directory")
            parent = f"{parent}/{part}" if parent else part
        assert found is not None
        return found

    async def _find_entry(self, path: str, *, expect_file: bool) -> _RemoteEntry:
        return await self._require_safe_chain(path, expect_file=expect_file)

    async def _find_child(self, parent: str, name: str) -> _RemoteEntry:
        for entry in await self._remote_list(parent):
            if entry.name == name:
                return entry
        raise PelicanOperationsError("not_found", "Requested item was not found")

    async def _remote_list(self, directory: str) -> tuple[_RemoteEntry, ...]:
        raise NotImplementedError

    async def _remote_read(self, path: str) -> bytes:
        raise NotImplementedError

    async def _remote_write(self, path: str, content: bytes) -> None:
        raise NotImplementedError

    async def _remote_rename(self, source: str, target: str) -> None:
        raise NotImplementedError

    async def _remote_delete(self, root: str, files: tuple[str, ...]) -> None:
        raise NotImplementedError

    async def _remote_upload(self, directory: str, filename: str, content: bytes) -> None:
        raise NotImplementedError

    async def _remote_backups(self) -> tuple[BackupEntry, ...]:
        raise NotImplementedError

    async def _remote_create_backup(self, name: str) -> str:
        raise NotImplementedError

    async def _remote_restore(self, backup_id: str, *, truncate: bool) -> None:
        raise NotImplementedError

    async def _remote_server_state(self) -> str:
        raise NotImplementedError

    async def _remote_power(self, signal: str) -> None:
        raise NotImplementedError


class PelicanOperationsHttpAdapter(_SafeOperations):
    """Production adapter using a server-side, secret-mounted Pelican token."""

    def __init__(
        self,
        base_url: str,
        token_file: Path,
        *,
        server_name: str = "LANtern Minecraft",
        virtual_host: str | None = None,
        allowed_upload_origins: frozenset[str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 30,
        backup_timeout_seconds: float = 300,
        poll_seconds: float = 2,
        clock: Callable[[], datetime] | None = None,
        consistency_registry: BackupConsistencyRegistry | None = None,
    ) -> None:
        super().__init__(
            backup_timeout_seconds=backup_timeout_seconds,
            poll_seconds=poll_seconds,
            clock=clock,
            consistency_registry=consistency_registry,
        )
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Pelican base URL must be an absolute HTTP URL")
        self._base_url = base_url.rstrip("/")
        self._token_file = Path(token_file)
        self._server_name = server_name
        if virtual_host is not None:
            host = urlsplit(f"http://{virtual_host}")
            try:
                port = host.port
            except ValueError as exc:
                raise ValueError("Pelican virtual host must be an exact host authority") from exc
            if (
                not host.hostname
                or host.netloc != virtual_host
                or not re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]{1,5})?", virtual_host)
                or (port is not None and not 1 <= port <= 65535)
                or host.username
                or host.password
                or host.path
                or host.query
                or host.fragment
            ):
                raise ValueError("Pelican virtual host must be an exact host authority")
        self._virtual_host = virtual_host
        self._server_id: str | None = None
        default_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        default_host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        upload_origins = allowed_upload_origins or frozenset(
            {f"{parsed.scheme}://{default_host}:{default_port}"}
        )
        self._allowed_upload_origins = frozenset(
            _exact_http_origin(origin, label="Pelican upload origin") for origin in upload_origins
        )
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            transport=transport,
            timeout=timeout_seconds,
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        try:
            token = self._token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise PelicanOperationsError(
                "configuration_error", "Pelican administration is not configured"
            ) from exc
        if len(token) < 16 or any(character.isspace() for character in token):
            raise PelicanOperationsError(
                "configuration_error", "Pelican administration is not configured"
            )
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if self._virtual_host is not None:
            headers["Host"] = self._virtual_host
        return headers

    async def _server_path(self, suffix: str) -> str:
        if self._server_id is None:
            payload = await self._json("GET", "/api/client")
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list):
                raise _invalid_response()
            matches = []
            for item in data:
                attrs = item.get("attributes") if isinstance(item, dict) else None
                if isinstance(attrs, dict) and attrs.get("name") == self._server_name:
                    identifier = attrs.get("identifier") or attrs.get("uuid")
                    if isinstance(identifier, str) and identifier:
                        matches.append(identifier)
            if len(matches) != 1:
                raise PelicanOperationsError(
                    "server_discovery_failed", "LANtern Minecraft was not found in Pelican"
                )
            self._server_id = matches[0]
        return f"/api/client/servers/{self._server_id}{suffix}"

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = kwargs.pop("headers", None) or self._headers()
        try:
            response = await self._client.request(method, path, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise PelicanOperationsError(
                "dependency_timeout", "Pelican did not finish the request in time"
            ) from exc
        except httpx.RequestError as exc:
            raise PelicanOperationsError(
                "dependency_unavailable", "Pelican administration is unavailable"
            ) from exc
        if response.status_code == 404:
            raise PelicanOperationsError("not_found", "Requested item was not found")
        if response.status_code in {401, 403}:
            raise PelicanOperationsError(
                "dependency_unauthorized", "Pelican administration is unavailable"
            )
        if response.status_code >= 400:
            raise PelicanOperationsError("dependency_failed", "Pelican refused the request")
        return response

    async def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._request(method, path, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise _invalid_response() from exc

    async def _remote_list(self, directory: str) -> tuple[_RemoteEntry, ...]:
        path = await self._server_path("/files/list")
        payload = await self._json("GET", path, params={"directory": f"/{directory}"})
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise _invalid_response()
        entries = []
        for item in data:
            attrs = item.get("attributes") if isinstance(item, dict) else None
            if not isinstance(attrs, dict) or not isinstance(attrs.get("name"), str):
                raise _invalid_response()
            size = attrs.get("size", 0)
            if not isinstance(size, int) or size < 0:
                raise _invalid_response()
            entries.append(
                _RemoteEntry(
                    name=attrs["name"],
                    is_file=bool(attrs.get("is_file", False)),
                    is_symlink=bool(attrs.get("is_symlink", False)),
                    byte_size=size,
                    modified_at=(str(attrs["modified_at"]) if attrs.get("modified_at") else None),
                )
            )
        return tuple(entries)

    async def _remote_read(self, path: str) -> bytes:
        endpoint = await self._server_path("/files/contents")
        response = await self._request("GET", endpoint, params={"file": f"/{path}"})
        return response.content

    async def _remote_write(self, path: str, content: bytes) -> None:
        endpoint = await self._server_path("/files/write")
        await self._request(
            "POST",
            endpoint,
            params={"file": f"/{path}"},
            content=content,
            headers={**self._headers(), "Content-Type": "text/plain; charset=utf-8"},
        )

    async def _remote_rename(self, source: str, target: str) -> None:
        endpoint = await self._server_path("/files/rename")
        source_path = PurePosixPath(source)
        target_path = PurePosixPath(target)
        if source_path.parent != target_path.parent:
            raise PelicanOperationsError(
                "unsafe_path", "Mod renames must remain in the mods folder"
            )
        await self._request(
            "PUT",
            endpoint,
            json={
                "root": f"/{source_path.parent.as_posix()}",
                "files": [{"from": source_path.name, "to": target_path.name}],
            },
        )

    async def _remote_delete(self, root: str, files: tuple[str, ...]) -> None:
        endpoint = await self._server_path("/files/delete")
        await self._request("POST", endpoint, json={"root": f"/{root}", "files": list(files)})

    async def _remote_upload(self, directory: str, filename: str, content: bytes) -> None:
        endpoint = await self._server_path("/files/upload")
        payload = await self._json("GET", endpoint)
        attrs = payload.get("attributes") if isinstance(payload, dict) else None
        upload_url = attrs.get("url") if isinstance(attrs, dict) else None
        if not isinstance(upload_url, str):
            raise _invalid_response()
        parsed = urlsplit(upload_url)
        upload_origin = f"{parsed.scheme}://{parsed.netloc}"
        if (
            upload_origin not in self._allowed_upload_origins
            or parsed.username
            or parsed.password
            or parsed.fragment
            or parsed.path != "/upload/file"
        ):
            raise PelicanOperationsError(
                "dependency_invalid_response", "Pelican returned an invalid upload target"
            )
        try:
            response = await self._client.post(
                upload_url,
                data={"directory": f"/{directory}"},
                files={"files": (filename, content, "application/java-archive")},
                headers={"Accept": "application/json"},
            )
        except httpx.TimeoutException as exc:
            raise PelicanOperationsError(
                "dependency_timeout", "Pelican did not finish the mod upload in time"
            ) from exc
        except httpx.RequestError as exc:
            raise PelicanOperationsError(
                "dependency_unavailable", "Pelican mod upload is unavailable"
            ) from exc
        if response.status_code >= 400:
            raise PelicanOperationsError("dependency_failed", "Pelican refused the mod upload")

    async def _remote_backups(self) -> tuple[BackupEntry, ...]:
        endpoint = await self._server_path("/backups")
        payload = await self._json("GET", endpoint)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise _invalid_response()
        backups = tuple(_backup_from_payload(item) for item in data)
        return tuple(
            replace(backup, consistency_proven=self._consistency_registry.contains(backup))
            for backup in backups
        )

    async def _remote_create_backup(self, name: str) -> str:
        endpoint = await self._server_path("/backups")
        payload = await self._json("POST", endpoint, json={"name": name, "is_locked": False})
        backup = _backup_from_payload(payload.get("data") if isinstance(payload, dict) else None)
        return backup.backup_id

    async def _remote_restore(self, backup_id: str, *, truncate: bool) -> None:
        endpoint = await self._server_path(f"/backups/{backup_id}/restore")
        await self._request("POST", endpoint, json={"truncate": truncate})

    async def _remote_server_state(self) -> str:
        endpoint = await self._server_path("/resources")
        payload = await self._json("GET", endpoint)
        attrs = payload.get("attributes") if isinstance(payload, dict) else None
        state = attrs.get("current_state") if isinstance(attrs, dict) else None
        if not isinstance(state, str) or state not in {
            "offline",
            "starting",
            "running",
            "stopping",
        }:
            raise _invalid_response()
        return state

    async def _remote_power(self, signal: str) -> None:
        if signal != "stop":
            raise PelicanOperationsError("unsafe_power", "Only a server stop is allowed here")
        endpoint = await self._server_path("/power")
        await self._request("POST", endpoint, json={"signal": signal})


class InMemoryPelicanOperations(_SafeOperations):
    """Deterministic adapter for tests and local portal development."""

    def __init__(
        self,
        *,
        files: Mapping[str, bytes] | None = None,
        symlinks: frozenset[str] = frozenset(),
        backups: Sequence[BackupEntry] = (),
        server_state: str = "offline",
        clock: Callable[[], datetime] | None = None,
        consistency_registry: BackupConsistencyRegistry | None = None,
    ) -> None:
        super().__init__(
            backup_timeout_seconds=0,
            poll_seconds=0,
            clock=clock,
            consistency_registry=consistency_registry,
        )
        self._files = {
            _clean_memory_path(path): bytes(value) for path, value in (files or {}).items()
        }
        self._symlinks = {_clean_memory_path(path) for path in symlinks}
        self._directories = {"mods"} | _memory_directories(set(self._files) | self._symlinks)
        self._versions = {path: 1 for path in self._files}
        self._backups = {backup.backup_id: backup for backup in backups}
        self._backup_sequence = len(self._backups)
        self._restored: list[str] = []
        self._server_state = server_state
        self._power_signals: list[str] = []
        for backup in backups:
            if backup.consistency_proven:
                self._consistency_registry.record(backup)

    @property
    def restored_backup_ids(self) -> tuple[str, ...]:
        return tuple(self._restored)

    @property
    def power_signals(self) -> tuple[str, ...]:
        return tuple(self._power_signals)

    async def _remote_list(self, directory: str) -> tuple[_RemoteEntry, ...]:
        prefix = f"{directory}/" if directory else ""
        children: dict[str, _RemoteEntry] = {}
        candidates = set(self._files) | self._symlinks | self._directories
        for path in candidates:
            if not path.startswith(prefix):
                continue
            remainder = path[len(prefix) :]
            if not remainder:
                continue
            name, separator, _tail = remainder.partition("/")
            child_path = f"{prefix}{name}" if prefix else name
            is_file = not separator and child_path in self._files
            size = len(self._files[child_path]) if is_file else 0
            version = self._versions.get(child_path, 1)
            children[name] = _RemoteEntry(
                name,
                is_file,
                child_path in self._symlinks,
                size,
                f"memory-{version}",
            )
        if (
            directory
            and not children
            and directory not in self._directories
            and directory not in _memory_directories(candidates)
        ):
            raise PelicanOperationsError("not_found", "Requested item was not found")
        return tuple(children.values())

    async def _remote_read(self, path: str) -> bytes:
        try:
            return self._files[path]
        except KeyError as exc:
            raise PelicanOperationsError("not_found", "Requested item was not found") from exc

    async def _remote_write(self, path: str, content: bytes) -> None:
        if path not in self._files:
            raise PelicanOperationsError("not_found", "Requested item was not found")
        self._files[path] = bytes(content)
        self._versions[path] = self._versions.get(path, 0) + 1

    async def _remote_rename(self, source: str, target: str) -> None:
        if source not in self._files:
            raise PelicanOperationsError("not_found", "Requested item was not found")
        if target in self._files:
            raise PelicanOperationsError("mod_exists", "The target mod state already exists")
        self._files[target] = self._files.pop(source)
        self._versions[target] = self._versions.pop(source, 0) + 1

    async def _remote_delete(self, root: str, files: tuple[str, ...]) -> None:
        for name in files:
            path = f"{root}/{name}"
            self._files.pop(path, None)
            self._versions.pop(path, None)

    async def _remote_upload(self, directory: str, filename: str, content: bytes) -> None:
        path = f"{directory}/{filename}"
        self._files[path] = bytes(content)
        self._versions[path] = self._versions.get(path, 0) + 1

    async def _remote_backups(self) -> tuple[BackupEntry, ...]:
        return tuple(
            replace(backup, consistency_proven=self._consistency_registry.contains(backup))
            for backup in self._backups.values()
        )

    async def _remote_create_backup(self, name: str) -> str:
        self._backup_sequence += 1
        backup_id = f"00000000-0000-4000-8000-{self._backup_sequence:012d}"
        digest = hashlib.sha256()
        for path, content in sorted(self._files.items()):
            digest.update(path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(content)
        backup = BackupEntry(
            backup_id,
            name,
            "ready",
            digest.hexdigest(),
            sum(len(content) for content in self._files.values()),
            self._clock().astimezone(UTC).isoformat(),
            name.startswith(_OFFLINE_BACKUP_PREFIX),
        )
        self._backups[backup_id] = backup
        return backup_id

    async def _remote_restore(self, backup_id: str, *, truncate: bool) -> None:
        if not truncate:
            raise PelicanOperationsError("unsafe_restore", "Restore must replace existing files")
        self._restored.append(backup_id)

    async def _remote_server_state(self) -> str:
        return self._server_state

    async def _remote_power(self, signal: str) -> None:
        self._power_signals.append(signal)
        if signal == "stop":
            self._server_state = "offline"


def _clean_path(path: str) -> str:
    if not isinstance(path, str) or not path or path.startswith(("/", "\\")) or "\\" in path:
        raise PelicanOperationsError("unsafe_path", "Path is outside the Minecraft allowlist")
    if any(ord(character) < 32 for character in path):
        raise PelicanOperationsError("unsafe_path", "Path is outside the Minecraft allowlist")
    parts = path.split("/")
    if any(not _safe_segment(part) for part in parts):
        raise PelicanOperationsError("unsafe_path", "Path is outside the Minecraft allowlist")
    return "/".join(parts)


def _safe_segment(segment: str) -> bool:
    return bool(segment and segment not in {".", ".."} and _SAFE_SEGMENT.fullmatch(segment))


def _exact_http_origin(value: str, *, label: str) -> str:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} must be an exact HTTP origin with an explicit port") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or port is None
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or value != f"{parsed.scheme}://{parsed.netloc}"
    ):
        raise ValueError(f"{label} must be an exact HTTP origin with an explicit port")
    return value


def _config_directory(path: str) -> str:
    if path == "":
        return ""
    clean = _clean_path(path)
    if not any(clean == root or clean.startswith(f"{root}/") for root in _CONFIG_ROOTS):
        raise PelicanOperationsError("unsafe_path", "Directory is outside the Minecraft allowlist")
    return clean


def _visible_config_directory(path: str) -> bool:
    return any(path == root or path.startswith(f"{root}/") for root in _CONFIG_ROOTS)


def _config_file(path: str) -> str:
    clean = _clean_path(path)
    allowed_root = any(clean.startswith(f"{root}/") for root in _CONFIG_ROOTS)
    if clean not in _CONFIG_FILES and not allowed_root:
        raise PelicanOperationsError("unsafe_path", "File is outside the Minecraft allowlist")
    if PurePosixPath(clean).suffix.lower() not in _TEXT_SUFFIXES:
        raise PelicanOperationsError("unsafe_path", "File type is outside the Minecraft allowlist")
    return clean


def _mod_name(filename: str) -> str:
    clean = _clean_path(filename)
    if (
        "/" in clean
        or not clean.lower().endswith(".jar")
        or clean.lower().endswith(".jar.disabled")
    ):
        raise PelicanOperationsError("invalid_mod_name", "Mod name must be a single .jar filename")
    return clean


def _stored_mod_name(filename: str) -> tuple[str, bool]:
    if filename.lower().endswith(".jar.disabled"):
        logical = filename[: -len(".disabled")]
        return _mod_name(logical), False
    return _mod_name(filename), True


def _expected_revision(value: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise PelicanOperationsError(
            "invalid_revision", "Expected revision must be a SHA-256 value"
        )
    return value


def _content_revision(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _validate_jar_expansion(archive: zipfile.ZipFile) -> None:
    """Reject structurally valid ZIP bombs before any member is decompressed."""
    members = archive.infolist()
    if len(members) > MAX_JAR_MEMBERS:
        raise PelicanOperationsError("invalid_mod", "Mod JAR contains too many entries")
    expanded = 0
    compressed = 0
    for member in members:
        if member.file_size < 0 or member.compress_size < 0:
            raise PelicanOperationsError("invalid_mod", "Mod JAR has invalid entry sizes")
        expanded += member.file_size
        compressed += member.compress_size
        if expanded > MAX_JAR_EXPANDED_BYTES:
            raise PelicanOperationsError("invalid_mod", "Mod JAR expands beyond the safe limit")
        if member.file_size and (
            member.compress_size == 0
            or member.file_size > member.compress_size * MAX_JAR_ENTRY_COMPRESSION_RATIO
        ):
            raise PelicanOperationsError(
                "invalid_mod", "Mod JAR contains an unsafe compression ratio"
            )
    if expanded and (compressed == 0 or expanded > compressed * MAX_JAR_TOTAL_COMPRESSION_RATIO):
        raise PelicanOperationsError("invalid_mod", "Mod JAR has an unsafe compression ratio")


def _entry_revision(stored_name: str, entry: _RemoteEntry) -> str:
    material = f"{stored_name}\0{entry.byte_size}\0{entry.modified_at or ''}".encode()
    return hashlib.sha256(material).hexdigest()


def _file_entry(path: str, entry: _RemoteEntry) -> FileEntry:
    return FileEntry(
        path,
        entry.name,
        "file" if entry.is_file else "directory",
        entry.byte_size,
        entry.modified_at,
        _entry_revision(path, entry),
    )


def _mod_entry(logical: str, enabled: bool, entry: _RemoteEntry) -> ModEntry:
    stored = logical if enabled else f"{logical}.disabled"
    return ModEntry(
        logical,
        enabled,
        entry.byte_size,
        entry.modified_at,
        _entry_revision(stored, entry),
    )


def _backup_name(name: str) -> str:
    clean = " ".join(name.split())
    if not clean or len(clean) > 120 or any(ord(character) < 32 for character in clean):
        raise PelicanOperationsError(
            "invalid_backup_name", "Backup name must be 1 to 120 characters"
        )
    return clean


def _backup_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9-]{8,64}", value):
        raise PelicanOperationsError("invalid_backup", "Backup identifier is invalid")
    return value


def _normalize_checksum(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    clean = value.lower().removeprefix("sha256:")
    return clean if _SHA256.fullmatch(clean) else None


def _backup_from_payload(item: Any) -> BackupEntry:
    attrs = item.get("attributes") if isinstance(item, dict) else None
    if not isinstance(attrs, dict):
        raise _invalid_response()
    backup_id = attrs.get("uuid")
    name = attrs.get("name")
    if not isinstance(backup_id, str) or not isinstance(name, str):
        raise _invalid_response()
    successful = attrs.get("is_successful")
    completed = attrs.get("completed_at")
    if completed and successful is True:
        state: Literal["pending", "ready", "failed"] = "ready"
    elif completed:
        state = "failed"
    else:
        state = "pending"
    size = attrs.get("bytes", 0)
    if not isinstance(size, int) or size < 0:
        raise _invalid_response()
    return BackupEntry(
        backup_id=_backup_id(backup_id),
        name=name[:120],
        state=state,
        checksum_sha256=_normalize_checksum(attrs.get("checksum") or attrs.get("sha256_hash")),
        byte_size=size,
        created_at=str(attrs["created_at"]) if attrs.get("created_at") else None,
        consistency_proven=name.startswith(_OFFLINE_BACKUP_PREFIX),
    )


def _require_verified_backup(backup: BackupEntry, label: str) -> None:
    _require_successful_checksum(backup, label)
    if not backup.consistency_proven or not backup.name.startswith(_OFFLINE_BACKUP_PREFIX):
        raise PelicanOperationsError(
            "backup_not_verified",
            f"{label} is not a successful, offline-consistent backup with a SHA-256 checksum",
        )


def _require_successful_checksum(backup: BackupEntry, label: str) -> None:
    if backup.state != "ready" or not backup.checksum_sha256:
        raise PelicanOperationsError(
            "backup_not_verified", f"{label} is not successful with a SHA-256 checksum"
        )


def _backup_sort_key(backup: BackupEntry) -> tuple[str, str]:
    return backup.created_at or "", backup.backup_id


def _invalid_response() -> PelicanOperationsError:
    return PelicanOperationsError(
        "dependency_invalid_response", "Pelican returned an invalid response"
    )


def _clean_memory_path(path: str) -> str:
    clean = path.strip("/")
    if not clean:
        raise ValueError("in-memory paths must not be empty")
    return clean


def _memory_directories(paths: set[str]) -> set[str]:
    directories: set[str] = set()
    for path in paths:
        parts = path.split("/")
        for index in range(1, len(parts)):
            directories.add("/".join(parts[:index]))
    return directories
