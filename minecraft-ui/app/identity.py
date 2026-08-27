"""Named administrator identities and signed browser sessions.

The public seam is :class:`NamedAdminIdentity`.  User storage varies behind the
small :class:`IdentityDirectory` interface: production reads a secret-mounted
JSON document, while tests can use the in-memory adapter.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from argon2 import PasswordHasher, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

COOKIE_NAME = "lantern_minecraft_admin"
DIRECTORY_SCHEMA_VERSION = 1
MAX_DIRECTORY_BYTES = 256 * 1024
MAX_USERS = 64
MIN_SESSION_TTL_SECONDS = 5 * 60
MAX_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_SESSION_TOKEN_BYTES = 4096

_USERNAME = re.compile(r"[a-z][a-z0-9_-]{2,31}\Z")
_ROLE = re.compile(r"[a-z][a-z0-9_-]{1,31}\Z")
_UPSTREAM_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_SESSION_ID = re.compile(r"[0-9a-f]{32}\Z")
_USER_FIELDS = {
    "username",
    "password_hash",
    "role",
    "upstream_alias",
    "credential_version",
}


class IdentityErrorCode(StrEnum):
    """Stable, safe error codes for callers and audit records."""

    INVALID_CREDENTIALS = "invalid_credentials"
    INVALID_SESSION = "invalid_session"
    EXPIRED_SESSION = "expired_session"
    REVOKED_SESSION = "revoked_session"


_ERROR_MESSAGES = {
    IdentityErrorCode.INVALID_CREDENTIALS: "invalid administrator credentials",
    IdentityErrorCode.INVALID_SESSION: "invalid administrator session",
    IdentityErrorCode.EXPIRED_SESSION: "administrator session expired",
    IdentityErrorCode.REVOKED_SESSION: "administrator session revoked",
}


class IdentityError(Exception):
    """A stable authentication failure that is safe to return to a caller."""

    def __init__(self, code: IdentityErrorCode) -> None:
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])


class IdentityConfigurationError(ValueError):
    """A stable startup/runtime error for an invalid identity directory."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class CookieMode(StrEnum):
    """Supported browser transports for administrator sessions."""

    SECURE = "secure"
    TRUSTED_LAN = "trusted-lan"


@dataclass(frozen=True, slots=True)
class AdminUser:
    """A validated administrator record loaded from the identity directory."""

    username: str
    password_hash: str
    role: str
    upstream_alias: str
    credential_version: int


@dataclass(frozen=True, slots=True)
class AdminPrincipal:
    """The current named identity represented by an authenticated session."""

    username: str
    role: str
    upstream_alias: str
    credential_version: int
    session_id: str
    issued_at: int
    expires_at: int


@dataclass(frozen=True, slots=True)
class SessionCookie:
    """Framework-neutral instructions for setting the administrator cookie."""

    name: str
    value: str
    max_age: int
    secure: bool
    httponly: bool = True
    samesite: str = "strict"
    path: str = "/"

    def response_kwargs(self) -> dict[str, str | int | bool]:
        """Return keyword arguments accepted by Starlette/FastAPI set_cookie."""
        return {
            "key": self.name,
            "value": self.value,
            "max_age": self.max_age,
            "secure": self.secure,
            "httponly": self.httponly,
            "samesite": self.samesite,
            "path": self.path,
        }


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    """A successful login result with its named principal and browser cookie."""

    principal: AdminPrincipal
    cookie: SessionCookie


class IdentityDirectory(Protocol):
    """Lookup interface at the identity-storage seam."""

    def find(self, username: str) -> AdminUser | None:
        """Return the current user record, or None when it is absent."""
        ...


class SessionRevocations(Protocol):
    """Server-side invalidation seam for signed browser sessions."""

    def is_revoked(self, session_id: str, *, now: int) -> bool: ...

    def revoke(self, session_id: str, *, expires_at: int) -> None: ...


class InMemorySessionRevocations:
    """Thread-safe revocation registry for tests and injected runtimes."""

    def __init__(self) -> None:
        self._entries: dict[str, int] = {}
        self._lock = threading.Lock()

    def is_revoked(self, session_id: str, *, now: int) -> bool:
        with self._lock:
            self._entries = {key: expiry for key, expiry in self._entries.items() if expiry > now}
            return session_id in self._entries

    def revoke(self, session_id: str, *, expires_at: int) -> None:
        with self._lock:
            self._entries[session_id] = expires_at


class SqliteSessionRevocations:
    """Persistent logout revocations shared by all portal workers."""

    def __init__(self, database: str) -> None:
        self._database = database
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS revoked_admin_sessions (
                    session_id TEXT PRIMARY KEY,
                    expires_at INTEGER NOT NULL
                )
                """
            )

    def is_revoked(self, session_id: str, *, now: int) -> bool:
        with self._connect() as connection:
            connection.execute("DELETE FROM revoked_admin_sessions WHERE expires_at <= ?", (now,))
            row = connection.execute(
                "SELECT 1 FROM revoked_admin_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return row is not None

    def revoke(self, session_id: str, *, expires_at: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO revoked_admin_sessions(session_id, expires_at) "
                "VALUES (?, ?)",
                (session_id, expires_at),
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._database, timeout=5)


class InMemoryIdentityDirectory:
    """Mutable adapter for interface-level tests and local composition."""

    def __init__(self, users: Iterable[AdminUser]) -> None:
        self._users: dict[str, AdminUser] = {}
        self.replace(users)

    def find(self, username: str) -> AdminUser | None:
        """Return a user from the current in-memory snapshot."""
        return self._users.get(username)

    def replace(self, users: Iterable[AdminUser]) -> None:
        """Atomically replace the directory after validating the full snapshot."""
        self._users = _validated_user_map(list(users))


class JsonFileIdentityDirectory:
    """Read-only adapter for a secret-mounted JSON identity document.

    The file is re-read for every lookup.  Updating the mount and incrementing a
    user's credential version therefore revokes existing sessions without an
    application restart.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._load()  # Fail closed during application startup.

    def find(self, username: str) -> AdminUser | None:
        """Return a user from the latest complete file snapshot."""
        return self._load().get(username)

    def _load(self) -> dict[str, AdminUser]:
        try:
            stat = self._path.stat()
            if not self._path.is_file() or stat.st_size > MAX_DIRECTORY_BYTES:
                raise OSError("identity directory is unavailable")
            raw = self._path.read_bytes()
        except OSError as exc:
            raise IdentityConfigurationError(
                "identity_directory_unavailable", "identity directory is unavailable"
            ) from exc

        if len(raw) > MAX_DIRECTORY_BYTES:
            raise IdentityConfigurationError(
                "identity_directory_invalid", "identity directory is invalid"
            )
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IdentityConfigurationError(
                "identity_directory_invalid", "identity directory is invalid"
            ) from exc
        return _parse_directory(document)


class NamedAdminIdentity:
    """Authenticate named admins and issue or resolve revocable sessions."""

    def __init__(
        self,
        directory: IdentityDirectory,
        *,
        session_secret: bytes,
        ttl_seconds: int,
        cookie_mode: CookieMode,
        clock: Callable[[], float] = time.time,
        session_id_factory: Callable[[], str] | None = None,
        revocations: SessionRevocations | None = None,
    ) -> None:
        if len(session_secret) < 32:
            raise IdentityConfigurationError(
                "session_secret_invalid", "session secret must contain at least 32 bytes"
            )
        if not MIN_SESSION_TTL_SECONDS <= ttl_seconds <= MAX_SESSION_TTL_SECONDS:
            raise IdentityConfigurationError(
                "session_ttl_invalid",
                "session lifetime must be between five minutes and seven days",
            )
        if not isinstance(cookie_mode, CookieMode):
            raise IdentityConfigurationError("cookie_mode_invalid", "cookie mode is invalid")

        self._directory = directory
        self._session_secret = bytes(session_secret)
        self._ttl_seconds = ttl_seconds
        self._cookie_mode = cookie_mode
        self._clock = clock
        self._session_id_factory = session_id_factory or (lambda: uuid.uuid4().hex)
        self._password_hasher = PasswordHasher()
        self._revocations = revocations or InMemorySessionRevocations()

    def authenticate(self, username: str, password: str) -> AuthenticatedSession:
        """Verify credentials and return a signed, named administrator session.

        Unknown users, malformed usernames, and wrong passwords deliberately
        share one stable error so the interface does not disclose membership.
        """
        normalized = _normalize_login_username(username)
        usable_password = isinstance(password, str) and 1 <= len(password) <= 1024
        user = self._directory.find(normalized) if normalized else None

        if user is None or not usable_password:
            # Spend Argon2 work on invalid identities too, reducing membership
            # leakage without keeping a credential-like dummy hash in source.
            self._password_hasher.hash("invalid-credential-probe")
            raise IdentityError(IdentityErrorCode.INVALID_CREDENTIALS)

        try:
            verified = self._password_hasher.verify(user.password_hash, password)
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            verified = False
        if not verified:
            raise IdentityError(IdentityErrorCode.INVALID_CREDENTIALS)

        now = int(self._clock())
        session_id = self._session_id_factory()
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            raise IdentityConfigurationError(
                "session_id_invalid", "session id factory returned an invalid value"
            )
        claims = {
            "sub": user.username,
            "role": user.role,
            "sid": session_id,
            "iat": now,
            "exp": now + self._ttl_seconds,
            "cv": user.credential_version,
        }
        token = self._sign(claims)
        principal = AdminPrincipal(
            username=user.username,
            role=user.role,
            upstream_alias=user.upstream_alias,
            credential_version=user.credential_version,
            session_id=session_id,
            issued_at=now,
            expires_at=now + self._ttl_seconds,
        )
        cookie = SessionCookie(
            name=COOKIE_NAME,
            value=token,
            max_age=self._ttl_seconds,
            secure=self._cookie_mode is CookieMode.SECURE,
        )
        return AuthenticatedSession(principal=principal, cookie=cookie)

    def resolve(self, token: str) -> AdminPrincipal:
        """Resolve a signed token against the current identity snapshot.

        Removing the user, changing their role, or incrementing their credential
        version revokes the session even when its signature and expiry are valid.
        """
        claims = self._verify_token(token)
        now = int(self._clock())
        if claims["exp"] <= now:
            raise IdentityError(IdentityErrorCode.EXPIRED_SESSION)
        if self._revocations.is_revoked(claims["sid"], now=now):
            raise IdentityError(IdentityErrorCode.REVOKED_SESSION)

        user = self._directory.find(claims["sub"])
        if user is None or user.credential_version != claims["cv"] or user.role != claims["role"]:
            raise IdentityError(IdentityErrorCode.REVOKED_SESSION)

        return AdminPrincipal(
            username=user.username,
            role=user.role,
            upstream_alias=user.upstream_alias,
            credential_version=user.credential_version,
            session_id=claims["sid"],
            issued_at=claims["iat"],
            expires_at=claims["exp"],
        )

    def revoke(self, token: str) -> None:
        """Persistently invalidate a signed session until its natural expiry."""
        claims = self._verify_token(token)
        now = int(self._clock())
        if claims["exp"] > now:
            self._revocations.revoke(claims["sid"], expires_at=claims["exp"])

    def _sign(self, claims: Mapping[str, str | int]) -> str:
        payload = json.dumps(
            claims, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        signature = hmac.new(self._session_secret, payload, hashlib.sha256).digest()
        return f"{_encode(payload)}.{_encode(signature)}"

    def _verify_token(self, token: str) -> dict[str, Any]:
        if not isinstance(token, str) or not token or len(token) > MAX_SESSION_TOKEN_BYTES:
            raise IdentityError(IdentityErrorCode.INVALID_SESSION)
        try:
            payload_text, signature_text = token.split(".")
            payload = _decode(payload_text)
            signature = _decode(signature_text)
        except (ValueError, binascii.Error):
            raise IdentityError(IdentityErrorCode.INVALID_SESSION) from None

        expected = hmac.new(self._session_secret, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise IdentityError(IdentityErrorCode.INVALID_SESSION)

        try:
            claims = json.loads(payload.decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise IdentityError(IdentityErrorCode.INVALID_SESSION) from None
        if not _valid_claims(claims):
            raise IdentityError(IdentityErrorCode.INVALID_SESSION)
        return claims


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    if not value or not value.isascii():
        raise ValueError("invalid base64url value")
    padded = value + "=" * (-len(value) % 4)
    return base64.b64decode(padded, altchars=b"-_", validate=True)


def _normalize_login_username(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip().casefold()
    return normalized if _USERNAME.fullmatch(normalized) else ""


def _strict_positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _valid_claims(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"sub", "role", "sid", "iat", "exp", "cv"}:
        return False
    if not isinstance(value["sub"], str) or not _USERNAME.fullmatch(value["sub"]):
        return False
    if not isinstance(value["role"], str) or not _ROLE.fullmatch(value["role"]):
        return False
    if not isinstance(value["sid"], str) or not _SESSION_ID.fullmatch(value["sid"]):
        return False
    if type(value["iat"]) is not int or not _strict_positive_int(value["exp"]):
        return False
    if not _strict_positive_int(value["cv"]):
        return False
    return 0 <= value["iat"] < value["exp"]


def _parse_directory(value: object) -> dict[str, AdminUser]:
    if not isinstance(value, dict) or set(value) != {"version", "users"}:
        raise IdentityConfigurationError(
            "identity_directory_invalid", "identity directory is invalid"
        )
    if type(value["version"]) is not int or value["version"] != DIRECTORY_SCHEMA_VERSION:
        raise IdentityConfigurationError(
            "identity_directory_version_unsupported",
            "identity directory version is unsupported",
        )
    raw_users = value["users"]
    if not isinstance(raw_users, list):
        raise IdentityConfigurationError(
            "identity_directory_invalid", "identity directory is invalid"
        )
    users: list[AdminUser] = []
    try:
        for raw_user in raw_users:
            if not isinstance(raw_user, dict) or set(raw_user) != _USER_FIELDS:
                raise ValueError
            users.append(
                AdminUser(
                    username=raw_user["username"],
                    password_hash=raw_user["password_hash"],
                    role=raw_user["role"],
                    upstream_alias=raw_user["upstream_alias"],
                    credential_version=raw_user["credential_version"],
                )
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise IdentityConfigurationError(
            "identity_directory_invalid", "identity directory is invalid"
        ) from exc
    return _validated_user_map(users)


def _validated_user_map(users: list[AdminUser]) -> dict[str, AdminUser]:
    if not 1 <= len(users) <= MAX_USERS:
        raise IdentityConfigurationError(
            "identity_directory_invalid", "identity directory is invalid"
        )
    result: dict[str, AdminUser] = {}
    hashes: set[str] = set()
    for user in users:
        try:
            parameters = extract_parameters(user.password_hash)
        except (InvalidHashError, TypeError) as exc:
            raise IdentityConfigurationError(
                "identity_directory_invalid", "identity directory is invalid"
            ) from exc
        if (
            not isinstance(user.username, str)
            or not _USERNAME.fullmatch(user.username)
            or not isinstance(user.role, str)
            or not _ROLE.fullmatch(user.role)
            or not isinstance(user.upstream_alias, str)
            or not _UPSTREAM_ALIAS.fullmatch(user.upstream_alias)
            or not _strict_positive_int(user.credential_version)
            or parameters.type is not Type.ID
            or user.username in result
            or user.password_hash in hashes
        ):
            raise IdentityConfigurationError(
                "identity_directory_invalid", "identity directory is invalid"
            )
        result[user.username] = user
        hashes.add(user.password_hash)
    return result
