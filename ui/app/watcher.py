"""Console watcher: turns in-game chat into loadout actions.

CS2 has no file-based console logging -- `-condebug` is inert and `con_logfile`
does not exist -- so the console has to be *streamed*. Rather than hand this
container the Docker socket just to read stdout, it uses the same websocket the
Pelican panel's own console uses: ask Pelican for a short-lived token, connect to
Wings, and read the stream.

Two jobs:

  1. Resolve SteamIDs. `status_json` reports players as steamid64 "0" (it simply
     does not populate the field), but the console prints the canonical
     `"name<slot><[U:1:1362677841]>"` on nearly every player event. That gives a
     reliable slot/name -> SteamID64 map, which the roster and the loadout picker
     both need.

  2. Handle `!1` .. `!9` in chat to apply a saved loadout preset.

The token expires; Wings warns first, and re-auth is handled without dropping
the connection.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from typing import Any

import httpx
import websockets

from . import presets, rcon

log = logging.getLogger("lantern.watcher")

PELICAN_URL = os.environ.get("PELICAN_URL", "").rstrip("/")
API_KEY = os.environ.get("PELICAN_API_KEY", "")
SERVER_UUID = os.environ.get("SERVER_UUID", "")
RCON_HOST = os.environ.get("RCON_HOST", "127.0.0.1")
RCON_PORT = int(os.environ.get("RCON_PORT", "27015"))
RCON_PASSWORD = os.environ.get("RCON_PASSWORD", "")

STEAM64_BASE = 76561197960265728

# "cyIVER<3><[U:1:1362677841]><CT>"  -- name, slot, account id
PLAYER_RE = re.compile(r'"(?P<name>[^"<]+)<(?P<slot>\d+)><\[U:1:(?P<acct>\d+)\]>')
# "[All Chat][cyIVER (1362677841)]: !1"
CHAT_RE = re.compile(r"\[(?:All|Team) Chat\]\[(?P<name>.+?) \((?P<acct>\d+)\)\]:\s*(?P<msg>.*)")
PRESET_RE = re.compile(r"^[!./](?P<slot>[1-9])\s*$")

# slot/name -> steamid64, learned from the console stream.
IDENTITIES: dict[str, str] = {}   # name -> steamid64
SLOTS: dict[int, str] = {}        # slot -> steamid64

# Proof-of-life for the stream: identities only appear when a player event
# occurs, so an empty map alone cannot distinguish "connected, quiet server"
# from "not connected at all".
STATS: dict[str, Any] = {"connected": False, "lines": 0, "last_line": "", "chat": 0}


def steam64(acct: str | int) -> str:
    """Convert a Steam account ID to its 64-bit SteamID."""
    return str(int(acct) + STEAM64_BASE)


def strip_ansi(s: str) -> str:
    """Remove ANSI escape sequences from console output."""
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


async def _ws_credentials(client: httpx.AsyncClient) -> tuple[str, str]:
    r = await client.get(
        f"{PELICAN_URL}/api/client/servers/{SERVER_UUID}/websocket",
        headers={"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json().get("data", {})
    return data["socket"], data["token"]


async def _say(message: str) -> None:
    with contextlib.suppress(Exception):
        await rcon.execute(RCON_HOST, RCON_PORT, RCON_PASSWORD, f'say {message}')


async def handle_line(raw: str) -> None:
    line = strip_ansi(raw)
    STATS["lines"] += 1
    STATS["last_line"] = line[-160:]

    for m in PLAYER_RE.finditer(line):
        sid = steam64(m.group("acct"))
        IDENTITIES[m.group("name")] = sid
        SLOTS[int(m.group("slot"))] = sid

    chat = CHAT_RE.search(line)
    if not chat:
        return
    STATS["chat"] += 1
    sid = steam64(chat.group("acct"))
    IDENTITIES[chat.group("name")] = sid

    pm = PRESET_RE.match(chat.group("msg").strip())
    if not pm:
        return

    slot = int(pm.group("slot"))
    name = chat.group("name")
    try:
        result = await asyncio.to_thread(presets.apply, sid, slot)
    except Exception as exc:                       # noqa: BLE001
        log.exception("preset apply failed")
        await _say(f"{name}: preset {slot} failed ({exc.__class__.__name__})")
        return

    if not result.get("ok"):
        await _say(f"{name}: no preset saved in slot {slot}")
        return
    # WeaponPaints re-reads on respawn; !wp refreshes without dying.
    await _say(f'{name}: loadout "{result["name"]}" applied - type !wp or respawn')


async def run() -> None:
    """Reconnecting console reader. Never raises; logs and retries."""
    if not (PELICAN_URL and API_KEY and SERVER_UUID):
        log.warning("watcher disabled: Pelican credentials not configured")
        return

    with contextlib.suppress(Exception):
        await asyncio.to_thread(presets.ensure_schema)

    # Pelican rate-limits the client API. Start well back and cap high: a
    # tight retry loop during an outage will trip a 429 and prolong it.
    backoff = 15
    async with httpx.AsyncClient() as client:
        while True:
            try:
                socket, token = await _ws_credentials(client)
                # Wings checks Origin and rejects anything that is not the panel
                # with a bare HTTP 403 before the upgrade completes.
                async with websockets.connect(
                    socket, ping_interval=20, open_timeout=15,
                    origin=PELICAN_URL,  # type: ignore[arg-type]
                ) as ws:
                    await ws.send(json.dumps({"event": "auth", "args": [token]}))
                    log.info("watcher connected to %s", socket)
                    STATS["connected"] = True
                    backoff = 15

                    async for message in ws:
                        try:
                            frame = json.loads(message)
                        except (TypeError, ValueError):
                            continue
                        event = frame.get("event")
                        args = frame.get("args") or []

                        if event in ("token expiring", "token expired", "jwt error"):
                            _, token = await _ws_credentials(client)
                            await ws.send(json.dumps({"event": "auth", "args": [token]}))
                            continue
                        if event == "console output" and args:
                            await handle_line(str(args[0]))
            except asyncio.CancelledError:
                raise
            except Exception as exc:               # noqa: BLE001
                STATS["connected"] = False
                log.warning("watcher disconnected (%s); retrying in %ss", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)


def snapshot() -> dict[str, Any]:
    """Return the current state of the console watcher: connection status, learned identities, and stats."""
    return {**STATS, "identities": dict(IDENTITIES),
            "slots": {str(k): v for k, v in SLOTS.items()}}
