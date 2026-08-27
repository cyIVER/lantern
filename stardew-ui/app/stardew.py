"""Stardew Valley control, over JunimoServer's HTTP API.

The pleasant surprise of this integration: unlike CS2 -- where the roster has to
be scraped out of two disagreeing `status` formats and moderation goes through
RCON -- JunimoServer exposes a real REST API with an OpenAPI spec. So this is a
thin, honest proxy rather than a parser.

Auth is not uniform. `/health` is deliberately open so a monitor can poll it;
every other route returns 401 without the bearer token. That distinction matters
because a 401 and a not-ready-yet server look identical if you only check for a
non-200, which is exactly the trap the setup script fell into.

Reached over the published host port rather than a shared Docker network: the
Stardew stack is its own compose project, and coupling the two would mean the
control UI could not start unless Stardew was also up.
"""
from __future__ import annotations

import base64
import os
from typing import Any

import httpx

BASE = os.environ.get("STARDEW_API_URL", "").rstrip("/")
KEY = os.environ.get("STARDEW_API_KEY", "")

TIMEOUT = 12.0


class StardewError(RuntimeError):
    pass


def configured() -> bool:
    return bool(BASE)


def _headers(auth: bool = True) -> dict[str, str]:
    h = {"Accept": "application/json"}
    if auth and KEY:
        h["Authorization"] = f"Bearer {KEY}"
    return h


async def call(method: str, path: str, *, auth: bool = True,
               raw: bool = False, **kw) -> Any:
    """One request against the Stardew API. Raises StardewError on failure."""
    if not BASE:
        raise StardewError("STARDEW_API_URL is not set; the Stardew server is not wired up")
    # Every POST here carries its arguments as query parameters and no body.
    # Without an explicit empty body httpx sends no Content-Length, and the
    # server answers 411 Length Required with a bare HTML error page.
    if method.upper() == "POST":
        kw.setdefault("content", b"")

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.request(method, f"{BASE}{path}", headers=_headers(auth), **kw)
    except httpx.RequestError as exc:
        raise StardewError(f"cannot reach the Stardew server ({exc.__class__.__name__})") from exc

    if r.status_code == 401:
        raise StardewError("401 from the Stardew API -- STARDEW_API_KEY is wrong or unset")
    if r.status_code >= 400:
        raise StardewError(f"{path} returned HTTP {r.status_code}: {r.text[:200]}")

    if raw:
        return r.content
    if not r.content:
        return {}
    try:
        return r.json()
    except ValueError:
        return {"raw": r.text[:500]}


# ------------------------------------------------------------------ read-only
async def health() -> dict[str, Any]:
    """Game-loop liveness. The only unauthenticated route."""
    return await call("GET", "/health", auth=False)


async def status() -> dict[str, Any]:
    return await call("GET", "/status")


async def players() -> dict[str, Any]:
    return await call("GET", "/players")


async def farmhands() -> dict[str, Any]:
    return await call("GET", "/farmhands")


async def settings() -> dict[str, Any]:
    return await call("GET", "/settings")


async def cabins() -> dict[str, Any]:
    return await call("GET", "/cabins")


async def stats() -> dict[str, Any]:
    return await call("GET", "/stats")


async def screenshot() -> bytes:
    """Decoded PNG bytes of the running game -- a live look without VNC.

    The endpoint is named /screenshot and its OpenAPI entry says it returns
    application/json, which it genuinely does: {success, base64Png, width,
    height}. Proxying the response straight through as image/png produces a
    broken image with no error anywhere, so decode it here.

    Only meaningful when the render rate is above zero; rendering is off by
    default because drawing frames nobody watches is pure waste.
    """
    d = await call("GET", "/screenshot")
    if not d.get("success") or not d.get("base64Png"):
        raise StardewError(d.get("error") or "the server returned no image "
                           "(set the render rate above 0 first)")
    try:
        return base64.b64decode(d["base64Png"])
    except (ValueError, TypeError) as exc:
        raise StardewError("the server returned an undecodable image") from exc


async def overview() -> dict[str, Any]:
    """Everything the dashboard needs, in one round trip from the browser.

    Each part is allowed to fail on its own: a farm that is still loading
    answers /health long before it can answer /status, and a partial dashboard
    is far more useful than a single error message.
    """
    out: dict[str, Any] = {"configured": True, "errors": {}}
    for name, fn in (("health", health), ("status", status),
                     ("players", players), ("cabins", cabins),
                     ("farmhands", farmhands), ("stats", stats),
                     ("settings", settings), ("rendering", get_rendering)):
        try:
            out[name] = await fn()
        except StardewError as exc:
            out[name] = None
            out["errors"][name] = str(exc)
    out["online"] = bool((out.get("health") or {}).get("gameAvailable"))
    return out


# ----------------------------------------------------------------- write side
async def get_rendering() -> dict[str, Any]:
    return await call("GET", "/rendering")


async def set_rendering(fps: int) -> dict[str, Any]:
    """0 disables rendering. Raise it only while someone is watching VNC or
    taking screenshots -- frames nobody sees still cost CPU."""
    return await call("POST", "/rendering", params={"fps": fps})


async def set_time(time_of_day: int) -> dict[str, Any]:
    """Stardew clock, in its own format: 600 is 6am, 2600 is 2am the next day.

    The query parameter is `value`, not `time`.
    """
    return await call("POST", "/time", params={"value": time_of_day})


async def set_clock_speed(multiplier: float) -> dict[str, Any]:
    """10 for ten times faster, 1 to restore. The parameter is `multiplier`."""
    return await call("POST", "/clock-speed", params={"multiplier": multiplier})


async def grant_admin(player: str) -> dict[str, Any]:
    """Accepts `name` or `playerId`; `player` is silently not a parameter."""
    return await call("POST", "/roles/admin", params={"name": player})


async def delete_farmhand(name: str) -> dict[str, Any]:
    """Frees a cabin slot. Destructive: it deletes that farmhand's character."""
    return await call("DELETE", "/farmhands", params={"name": name})


async def reload() -> dict[str, Any]:
    """Re-read server-settings.json and reload the world, without a restart."""
    return await call("POST", "/reload")
