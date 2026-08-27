"""Talk to the LANtern control service about starting and stopping servers.

WHY PROXY RATHER THAN DO IT HERE

This container already has the Docker socket, so it could start and stop the
Stardew containers directly in about ten lines. It deliberately does not.

Only one game server may run at a time -- the VM cannot hold two -- and that
rule has to be enforced somewhere. Implementing it in both control UIs means
two copies of a safety rule that must not disagree, and the way they would
disagree is that one of them starts Stardew without stopping CS2 and the
kernel kills something mid-save. So the rule lives in the LANtern service, and
this forwards to it.

Forwarding server-side rather than calling from the browser also avoids CORS:
the two UIs are on different ports, so a fetch from this page to :8090 is
cross-origin and would need the other service to opt in. It has no reason to.

The 409 that means "this would stop CS2, confirm first" is passed through with
its body intact, so this UI can show the same confirmation the landing page
does rather than inventing a second dialect.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

# The LANtern service is a different compose project, so it cannot be reached
# by service name. host.docker.internal maps to the VM, where its port is
# published; compose adds the host-gateway entry that makes that resolve.
LANTERN_URL = os.environ.get("LANTERN_UI_URL", "http://host.docker.internal:8090").rstrip("/")
TIMEOUT = float(os.environ.get("LANTERN_TIMEOUT", "20"))


class LanternError(RuntimeError):
    """The control service could not be reached or refused."""

    def __init__(self, message: str, status: int = 503, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.detail = detail


async def _call(method: str, path: str, **kw) -> Any:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.request(method, f"{LANTERN_URL}{path}", **kw)
    except httpx.HTTPError as exc:
        raise LanternError(
            f"the LANtern control service at {LANTERN_URL} did not answer: {exc}"
        ) from exc

    if r.status_code >= 400:
        # Keep the structured body. A 409 from /start carries what it would
        # have to shut down, and that is the whole message the operator needs.
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text[:300]
        message = detail.get("message") if isinstance(detail, dict) else str(detail)
        raise LanternError(message or f"HTTP {r.status_code}",
                           status=r.status_code, detail=detail)
    return r.json() if r.content else None


async def servers() -> dict[str, Any]:
    """State of every game server, plus which are running."""
    return await _call("GET", "/api/servers")


async def start(game: str = "stardew", confirm: bool = False) -> dict[str, Any]:
    return await _call("POST", f"/api/servers/{game}/start", json={"confirm": confirm})


async def stop(game: str = "stardew") -> dict[str, Any]:
    return await _call("POST", f"/api/servers/{game}/stop", json={})


async def reachable() -> bool:
    try:
        await servers()
        return True
    except LanternError:
        return False
