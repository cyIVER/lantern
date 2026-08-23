"""LANtern CS2 control UI -- backend.

Hybrid by design:
  * Pelican client API  -> power, startup variables (things RCON cannot do,
    because RCON only reaches a *running* server and knows nothing about the
    egg's configuration).
  * Direct RCON         -> live roster, moderation, cvars (things Pelican
    cannot do, because its command endpoint returns no output).

Both paths act on the same server, so the panel and this UI stay in sync.
"""
from __future__ import annotations

import json
import os
import re
import pathlib
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import loadout, rcon

STATIC = pathlib.Path(__file__).parent.parent / "static"

PELICAN_URL = os.environ.get("PELICAN_URL", "http://panel").rstrip("/")
API_KEY = os.environ.get("PELICAN_API_KEY", "")
SERVER_UUID = os.environ.get("SERVER_UUID", "")
RCON_HOST = os.environ.get("RCON_HOST", "127.0.0.1")
RCON_PORT = int(os.environ.get("RCON_PORT", "27015"))
RCON_PASSWORD = os.environ.get("RCON_PASSWORD", "")

# practice omitted: CSPracc segfaults current CS2. See docs/USING.md.
MODES = ["competitive", "retakes", "deathmatch"]

MODE_BLURB = {
    "competitive": "MR12 · knife round · MatchZy",
    "retakes":     "Continuous retakes",
    "deathmatch":  "Vanilla DM",
}

app = FastAPI(title="LANtern CS2 Control", docs_url=None, redoc_url=None)


# --------------------------------------------------------------- Pelican API
def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


async def pelican(method: str, path: str, **kw) -> Any:
    url = f"{PELICAN_URL}/api/client/servers/{SERVER_UUID}{path}"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.request(method, url, headers=_headers(), **kw)
    if r.status_code >= 400:
        raise HTTPException(r.status_code, f"pelican {path}: {r.text[:300]}")
    return r.json() if r.content and r.headers.get("content-type", "").startswith("application/json") else None


# ------------------------------------------------------------------- helpers
# CS2 exposes 'status_json', which returns server.clients as a structured array
# with steamid64, a bot flag and the name. That is far more durable than parsing
# the 'status' text table, whose columns shift between CS2 updates and which
# does not even include SteamIDs for every client.
def parse_status_json(text: str) -> dict[str, Any]:
    try:
        doc = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    srv = doc.get("server", {}) or {}
    players = []
    for c in srv.get("clients", []) or []:
        # Slots mid-connect report steamid64 "0" and a blank name; they are not
        # real clients and flicker in and out of the list.
        if not c.get("name") or str(c.get("steamid64")) in ("0", "None"):
            continue
        players.append({
            "name": c.get("name", "?"),
            "steamid64": c.get("steamid64"),
            "steamid": c.get("steamid"),
            "bot": bool(c.get("bot")),
        })
    return {
        "players": players,
        "map": srv.get("map"),
        "humans": srv.get("clients_human", 0),
        "bots": srv.get("clients_bot", 0),
        "hibernating": srv.get("hibernating", False),
        "server_cpu": round(srv.get("cpu_usage", 0) * 100, 1),
        "mem_avail_gb": round(doc.get("mem_phys_avail_gb", 0), 2),
    }


# -------------------------------------------------------------------- models
class CommandBody(BaseModel):
    command: str


class PowerBody(BaseModel):
    signal: str


class MapBody(BaseModel):
    map: str
    persist: bool = True


class ModeBody(BaseModel):
    mode: str


class PlayerAction(BaseModel):
    steamid64: str | None = None
    name: str | None = None
    bot: bool = False
    action: str
    duration: int = 0
    reason: str = ""


class VarBody(BaseModel):
    key: str
    value: str


# ------------------------------------------------------------------ endpoints
@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "server": SERVER_UUID[:8], "rcon": f"{RCON_HOST}:{RCON_PORT}"}


@app.get("/api/state")
async def state() -> dict[str, Any]:
    res = await pelican("GET", "/resources")
    attr = (res or {}).get("attributes", {})
    used = attr.get("resources", {})
    return {
        "state": attr.get("current_state", "unknown"),
        "cpu": round(used.get("cpu_absolute", 0), 1),
        "memory_mb": round(used.get("memory_bytes", 0) / 1048576),
        "uptime_s": round(used.get("uptime", 0) / 1000),
        "net_rx": used.get("network_rx_bytes", 0),
        "net_tx": used.get("network_tx_bytes", 0),
    }


@app.get("/api/config")
async def config() -> dict[str, Any]:
    data = await pelican("GET", "/startup")
    variables = {}
    for item in (data or {}).get("data", []):
        a = item.get("attributes", {})
        variables[a.get("env_variable")] = {
            "value": a.get("server_value"),
            "name": a.get("name"),
            "rules": a.get("rules", ""),
        }
    maps = sorted(p.stem for p in (STATIC / "maps").glob("*.svg"))
    allowed = []
    rules = variables.get("SRCDS_MAP", {}).get("rules", "") or ""
    m = re.search(r"in:([^|]+)", rules)
    if m:
        allowed = [x.strip() for x in m.group(1).split(",")]
    return {
        "variables": variables,
        "modes": [{"id": k, "blurb": MODE_BLURB[k]} for k in MODES],
        "maps": allowed or maps,
        "have_icon": maps,
    }


@app.get("/api/players")
async def players() -> dict[str, Any]:
    try:
        text = await rcon.execute(RCON_HOST, RCON_PORT, RCON_PASSWORD, "status_json")
    except rcon.RconError as exc:
        return {"players": [], "error": str(exc)}
    data = parse_status_json(text)
    if not data:
        return {"players": [], "error": "could not parse status_json"}
    return data


@app.post("/api/command")
async def command(body: CommandBody) -> dict[str, Any]:
    """Run a command via RCON so the output can be shown back to the user."""
    try:
        out = await rcon.execute(RCON_HOST, RCON_PORT, RCON_PASSWORD, body.command)
        return {"ok": True, "output": out}
    except rcon.RconError as exc:
        # Fall back to Pelican, which works even when RCON is unavailable.
        await pelican("POST", "/command", json={"command": body.command})
        return {"ok": True, "output": "", "note": f"sent via Pelican ({exc})"}


@app.post("/api/power")
async def power(body: PowerBody) -> dict[str, Any]:
    if body.signal not in {"start", "stop", "restart", "kill"}:
        raise HTTPException(400, "bad signal")
    await pelican("POST", "/power", json={"signal": body.signal})
    return {"ok": True, "signal": body.signal}


@app.post("/api/map")
async def change_map(body: MapBody) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9_]+", body.map):
        raise HTTPException(400, "bad map name")
    result: dict[str, Any] = {"map": body.map}
    if body.persist:
        await pelican("PUT", "/startup/variable",
                      json={"key": "SRCDS_MAP", "value": body.map})
        result["persisted"] = True
    try:
        await rcon.execute(RCON_HOST, RCON_PORT, RCON_PASSWORD, f"changelevel {body.map}")
        result["switched"] = True
    except rcon.RconError as exc:
        result["switched"] = False
        result["note"] = f"server not reachable over RCON ({exc}); will apply on next start"
    return result


@app.post("/api/mode")
async def change_mode(body: ModeBody) -> dict[str, Any]:
    if body.mode not in MODES:
        raise HTTPException(400, "unknown mode")
    # Mode selects which plugins load, which only happens at boot.
    await pelican("PUT", "/startup/variable", json={"key": "MODE", "value": body.mode})
    await pelican("POST", "/power", json={"signal": "restart"})
    return {"ok": True, "mode": body.mode, "restarting": True}


@app.put("/api/variable")
async def set_variable(body: VarBody) -> dict[str, Any]:
    await pelican("PUT", "/startup/variable", json={"key": body.key, "value": body.value})
    return {"ok": True, "key": body.key, "value": body.value}


@app.post("/api/player")
async def player_action(body: PlayerAction) -> dict[str, Any]:
    """Moderation. Prefers SteamID64 -- names are ambiguous and spoofable."""
    sid = body.steamid64
    name = (body.name or "").replace('"', "'")[:64]
    reason = body.reason.replace('"', "'")[:80]

    # Bots get synthetic 9007... ids that no admin command understands, so they
    # are handled by name through the engine's own bot_kick.
    if body.bot:
        if body.action != "kick":
            raise HTTPException(400, "bots only support kick")
        cmd = f'bot_kick "{name}"'
    elif body.action == "kick":
        cmd = f'css_kick {sid} "{reason}"'
    elif body.action == "ban":
        cmd = f'css_addban {sid} {body.duration} "{reason}"'
    elif body.action == "mute":
        cmd = f'css_addgag {sid} {body.duration} "{reason}"'
    elif body.action == "unmute":
        cmd = f"css_ungag {sid}"
    elif body.action == "slay":
        cmd = f"css_slay {sid}"
    elif body.action == "swap":
        cmd = f"css_swap {sid}"
    else:
        raise HTTPException(400, f"unknown action {body.action}")

    try:
        out = await rcon.execute(RCON_HOST, RCON_PORT, RCON_PASSWORD, cmd)
        return {"ok": True, "command": cmd, "output": out}
    except rcon.RconError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/api/match/{action}")
async def match(action: str) -> dict[str, Any]:
    """MatchZy controls. Only meaningful in competitive mode."""
    mapping = {
        "start":   "matchzy_start",
        "knife":   "css_knife",
        "pause":   "css_pause",
        "unpause": "css_unpause",
        "restart": "mp_restartgame 1",
        "warmup":  "mp_warmup_start",
    }
    if action not in mapping:
        raise HTTPException(400, "unknown match action")
    try:
        out = await rcon.execute(RCON_HOST, RCON_PORT, RCON_PASSWORD, mapping[action])
        return {"ok": True, "output": out}
    except rcon.RconError as exc:
        raise HTTPException(503, str(exc)) from exc


# ------------------------------------------------------------------- loadout
class KnifeBody(BaseModel):
    steamid64: str
    weapon_name: str


class GloveBody(BaseModel):
    steamid64: str
    weapon_defindex: int
    paint: int


class SkinBody(BaseModel):
    steamid64: str
    weapon_defindex: int
    paint: int
    wear: float = 0.0
    seed: int = 0
    stattrak: bool = False


def _valid_steamid(sid: str) -> str:
    if not (sid.isdigit() and len(sid) == 17 and sid.startswith("7656119")):
        raise HTTPException(400, f"not a SteamID64: {sid!r}")
    return sid


@app.get("/api/loadout/health")
async def loadout_health() -> dict[str, Any]:
    return loadout.health()


@app.get("/api/loadout/catalog/knives")
async def catalog_knives() -> list[dict[str, Any]]:
    return loadout.knives()


@app.get("/api/loadout/catalog/gloves")
async def catalog_gloves() -> list[dict[str, Any]]:
    return loadout.gloves()


@app.get("/api/loadout/catalog/weapons")
async def catalog_weapons() -> list[dict[str, Any]]:
    return loadout.weapons()


@app.get("/api/loadout/catalog/skins/{weapon_name}")
async def catalog_skins(weapon_name: str) -> list[dict[str, Any]]:
    if not re.fullmatch(r"weapon_[a-z0-9_]+", weapon_name):
        raise HTTPException(400, "bad weapon name")
    return loadout.skins_for(weapon_name)


@app.get("/api/loadout/{steamid64}")
async def loadout_current(steamid64: str) -> dict[str, Any]:
    return loadout.current(_valid_steamid(steamid64))


@app.post("/api/loadout/knife")
async def loadout_knife(body: KnifeBody) -> dict[str, Any]:
    if not re.fullmatch(r"weapon_[a-z0-9_]+", body.weapon_name):
        raise HTTPException(400, "bad weapon name")
    loadout.set_knife(_valid_steamid(body.steamid64), body.weapon_name)
    return {"ok": True, "knife": body.weapon_name, "note": "respawn to see it (!kill)"}


@app.post("/api/loadout/gloves")
async def loadout_gloves(body: GloveBody) -> dict[str, Any]:
    loadout.set_gloves(_valid_steamid(body.steamid64), body.weapon_defindex, body.paint)
    return {"ok": True, "note": "respawn to see it (!kill)"}


@app.post("/api/loadout/skin")
async def loadout_skin(body: SkinBody) -> dict[str, Any]:
    loadout.set_skin(_valid_steamid(body.steamid64), body.weapon_defindex,
                     body.paint, body.wear, body.seed, body.stattrak)
    return {"ok": True, "note": "respawn or re-buy to see it"}


@app.delete("/api/loadout/{steamid64}")
async def loadout_clear(steamid64: str) -> dict[str, Any]:
    return {"ok": True, "removed": loadout.clear(_valid_steamid(steamid64))}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
