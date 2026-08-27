"""Exact browser origin and Host policy for the Minecraft portal."""

from __future__ import annotations

import hmac
from collections.abc import Iterable
from urllib.parse import urlsplit

from fastapi import HTTPException, Request


class BrowserTrustPolicy:
    """Validate browser requests against an explicit deployment allowlist.

    ``allowed_origins=None`` is intentionally limited to dependency-injected
    tests and legacy adapters. Production composition always supplies the
    configured origins, preventing a forged Host header from defining its own
    trusted origin.
    """

    def __init__(self, allowed_origins: Iterable[str] | None = None) -> None:
        self._origins: tuple[str, ...] | None = None
        self._hosts: frozenset[str] | None = None
        if allowed_origins is None:
            return
        origins = tuple(allowed_origins)
        if not origins:
            raise ValueError("at least one trusted browser origin is required")
        hosts: set[str] = set()
        for origin in origins:
            parsed = urlsplit(origin)
            try:
                port = parsed.port
            except ValueError as exc:
                raise ValueError("trusted browser origins must be exact http(s) origins") from exc
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or port is None
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
                or origin != f"{parsed.scheme}://{parsed.netloc}"
            ):
                raise ValueError("trusted browser origins must be exact http(s) origins")
            hosts.add(parsed.netloc.casefold())
        self._origins = origins
        self._hosts = frozenset(hosts)

    def require_host(self, request: Request) -> None:
        """Reject a Host header outside the production deployment allowlist."""
        if self._hosts is None:
            return
        host = request.headers.get("host", "").casefold()
        if host not in self._hosts:
            raise HTTPException(400, "untrusted request host")

    def require_same_origin(self, request: Request) -> None:
        """Require both an allowed Host and an exact configured Origin."""
        self.require_host(request)
        origin = request.headers.get("origin", "")
        if self._origins is None:
            expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
            accepted = hmac.compare_digest(origin, expected)
        else:
            host = request.headers.get("host", "").casefold()
            accepted = any(
                hmac.compare_digest(origin, item) and urlsplit(item).netloc.casefold() == host
                for item in self._origins
            )
        if not accepted:
            raise HTTPException(403, "same-origin request required")
