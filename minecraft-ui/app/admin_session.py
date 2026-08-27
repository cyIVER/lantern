"""Self-contained administrator session for schematic-library curation."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import time
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import HTTPException, Request, Response

from .settings import Settings

COOKIE_NAME = "lantern_minecraft_admin"


class LoginRateLimiter:
    def __init__(self, *, attempts: int = 5, window_seconds: int = 300) -> None:
        """Initialize the rate limiter with maximum attempts and time window."""
        self._attempts = attempts
        self._window = window_seconds
        self._failures: dict[str, list[float]] = {}

    def retry_after(self, client_host: str) -> int | None:
        """Return seconds until the client may retry, or None if attempts remain."""
        now = time.monotonic()
        recent = [
            value
            for value in self._failures.get(client_host, [])
            if now - value < self._window
        ]
        self._failures[client_host] = recent
        if len(recent) < self._attempts:
            return None
        return max(1, math.ceil(self._window - (now - recent[0])))

    def failed(self, client_host: str) -> None:
        """Record a failed login attempt for rate limiting."""
        self._failures.setdefault(client_host, []).append(time.monotonic())

    def succeeded(self, client_host: str) -> None:
        """Clear failed attempts after a successful login."""
        self._failures.pop(client_host, None)


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(frozen=True, slots=True)
class AdminSessionAccess:
    password_hash: str | None
    session_secret: bytes | None
    viewer_token: str | None
    ttl_seconds: int
    secure_cookie: bool
    allow_insecure_admin: bool

    @classmethod
    def from_settings(cls, settings: Settings) -> AdminSessionAccess:
        """Construct AdminSessionAccess from application settings and secret files."""
        paths = (
            settings.admin_password_hash_file,
            settings.session_secret_file,
            settings.viewer_admin_token_file,
        )
        if not any(paths):
            return cls(
                None,
                None,
                None,
                settings.session_ttl_seconds,
                settings.secure_cookie,
                settings.allow_insecure_admin,
            )
        if not all(paths):
            raise ValueError("admin auth requires password hash, session secret, and viewer token files")
        password_hash = paths[0].read_text(encoding="utf-8").strip()  # type: ignore[union-attr]
        session_secret = paths[1].read_bytes().strip()  # type: ignore[union-attr]
        viewer_token = paths[2].read_text(encoding="utf-8").strip()  # type: ignore[union-attr]
        if len(session_secret) < 32 or len(viewer_token.encode("utf-8")) < 32:
            raise ValueError("session secret and viewer token must each contain at least 32 bytes")
        if settings.session_ttl_seconds < 300:
            raise ValueError("admin session lifetime must be at least five minutes")
        return cls(
            password_hash,
            session_secret,
            viewer_token,
            settings.session_ttl_seconds,
            settings.secure_cookie,
            settings.allow_insecure_admin,
        )

    @property
    def enabled(self) -> bool:
        """Whether credentials are complete and secure/test transport is allowed."""
        return self.session_secret is not None and (
            self.secure_cookie or self.allow_insecure_admin
        )

    def verify_password(self, password: str) -> bool:
        """Verify the provided password against the stored Argon2 hash."""
        if not self.enabled or not self.password_hash:
            return False
        try:
            return PasswordHasher().verify(self.password_hash, password)
        except (InvalidHashError, VerifyMismatchError):
            return False

    def issue(self, response: Response) -> None:
        """Issue a signed session cookie with the configured TTL."""
        if not self.enabled or not self.session_secret:
            raise HTTPException(503, "administrator login is disabled")
        payload = json.dumps(
            {"exp": int(time.time()) + self.ttl_seconds}, separators=(",", ":")
        ).encode("ascii")
        signature = hmac.new(self.session_secret, payload, hashlib.sha256).digest()
        response.set_cookie(
            COOKIE_NAME,
            f"{_encode(payload)}.{_encode(signature)}",
            max_age=self.ttl_seconds,
            httponly=True,
            samesite="strict",
            secure=self.secure_cookie,
            path="/",
        )

    def clear(self, response: Response) -> None:
        """Clear the session cookie to log out."""
        response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict")

    def is_admin(self, request: Request) -> bool:
        """Check whether the request carries a valid, unexpired admin session."""
        if not self.enabled or not self.session_secret:
            return False
        token = request.cookies.get(COOKIE_NAME, "")
        try:
            payload_text, signature_text = token.split(".", 1)
            payload = _decode(payload_text)
            signature = _decode(signature_text)
            expected = hmac.new(self.session_secret, payload, hashlib.sha256).digest()
            data = json.loads(payload)
            return hmac.compare_digest(signature, expected) and int(data["exp"]) >= time.time()
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            return False

    def require_same_origin(self, request: Request) -> None:
        """Enforce same-origin policy for mutation requests."""
        origin = request.headers.get("origin", "")
        expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
        if not hmac.compare_digest(origin, expected):
            raise HTTPException(403, "same-origin request required")

    def viewer_credential(self, request: Request, path: str) -> str | None:
        """Return the viewer admin token if the request is an authenticated admin session."""
        del path  # Authorization is request-scoped; the viewer owns route policy.
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            self.require_same_origin(request)
        return self.viewer_token if self.is_admin(request) else None
