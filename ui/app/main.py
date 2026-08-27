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

import asyncio
import contextlib
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

from . import host, loadout, presets, rcon, servers, watcher

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

@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    # The console watcher resolves SteamIDs and handles !1..!9 preset commands.
    task = asyncio.create_task(watcher.run())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="LANtern CS2 Control", docs_url=None, redoc_url=None,
              lifespan=lifespan)


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
    return {
        "clients": srv.get("clients", []) or [],
        "map": srv.get("map"),
        "humans": srv.get("clients_human", 0),
        "bots": srv.get("clients_bot", 0),
        "hibernating": srv.get("hibernating", False),
        "server_cpu": round(srv.get("cpu_usage", 0) * 100, 1),
        "mem_avail_gb": round(doc.get("mem_phys_avail_gb", 0), 2),
    }


# The text table is the authority on WHO is connected, because status_json
# reports players without a validated SteamID as name "" / steamid64 0 -- which
# is every player when sv_lan 1 skips Steam auth, e.g. anyone reaching the
# server through the Docker bridge. Rows look like:
#
#      3    03:17    1    0     active 786432 172.23.0.1:42167 'cyIVER'
#      0      BOT    0    0     active      0 'SourceTV'
#  65535 [NoChan]    0    0 challenging      0unknown ''        <- a real ghost
#
# Anchor on the leading slot id and the trailing quoted name; the middle columns
# run together (`0unknown`) and shift between CS2 versions.
STATUS_ROW = re.compile(r"^\s*(?P<slot>\d+)\s+(?P<time>\S+)\s+.*?'(?P<name>[^']*)'\s*$", re.M)


def parse_status_text(text: str) -> list[dict[str, Any]]:
    block = text
    start = text.find("---------players")
    if start >= 0:
        end = text.find("#end", start)
        block = text[start:end if end > 0 else None]

    rows = []
    for m in STATUS_ROW.finditer(block):
        slot = int(m.group("slot"))
        name = m.group("name")
        # 65535 is the placeholder slot for a connection still challenging.
        if slot == 65535 or not name:
            continue
        rows.append({"slot": slot, "name": name, "bot": m.group("time") == "BOT"})
    return rows


# CS2 will not hand out a SteamID for a player it never authenticated: under
# sv_lan the engine skips Steam auth, so status_json reports steamid64 "0" and
# the text table has no SteamID column at all. CS2-SimpleAdmin's css_players
# does know, and answers over RCON:
#
#   • [#3] "cyIVER" (IP Address: "172.23.0.1" SteamID64: "76561199322943569")
#
# This is a *pull*, so unlike the console watcher it cannot go stale: the
# watcher only learns an id when a player event happens to fly past, which means
# a UI restart leaves everyone already connected anonymous until they reconnect.
CSS_PLAYER_RE = re.compile(
    r'\[#(?P<slot>\d+)\]\s+"(?P<name>[^"]*)".*?SteamID64:\s*"(?P<sid>\d+)"')


async def resolve_identities() -> dict[int, str]:
    """Ask CS2-SimpleAdmin who is connected. Returns slot -> steamid64."""
    try:
        out = await rcon.execute(RCON_HOST, RCON_PORT, RCON_PASSWORD, "css_players")
    except rcon.RconError:
        return {}
    found: dict[int, str] = {}
    for m in CSS_PLAYER_RE.finditer(out):
        sid = m.group("sid")
        if sid == "0" or len(sid) != 17:
            continue
        slot = int(m.group("slot"))
        found[slot] = sid
        # Seed the watcher too, so chat presets work for a player who has not
        # said anything since the UI last restarted.
        watcher.SLOTS[slot] = sid
        watcher.IDENTITIES[m.group("name")] = sid
    return found


def merge_roster(json_text: str, status_text: str,
                 extra: dict[int, str] | None = None) -> dict[str, Any]:
    info = parse_status_json(json_text)
    by_name = {c.get("name"): c for c in info.get("clients", []) if c.get("name")}

    players = []
    for row in parse_status_text(status_text):
        c = by_name.get(row["name"], {})
        sid = c.get("steamid64")
        if sid in ("0", 0, None):
            # status_json leaves this blank for most clients on a LAN server.
            # Prefer the authoritative css_players lookup, then whatever the
            # console watcher happened to see.
            sid = ((extra or {}).get(row["slot"])
                   or watcher.IDENTITIES.get(row["name"])
                   or watcher.SLOTS.get(row["slot"]))
        players.append({
            "slot": row["slot"],
            "name": row["name"],
            "bot": row["bot"] or bool(c.get("bot")),
            "steamid64": sid,
            # Without a SteamID we can still act by slot, but not ban or set a
            # loadout -- both are keyed on the SteamID.
            "identified": sid is not None,
        })

    return {
        "players": players,
        "map": info.get("map"),
        "humans": info.get("humans", 0),
        "bots": info.get("bots", 0),
        "hibernating": info.get("hibernating", False),
        "server_cpu": info.get("server_cpu", 0),
        "mem_avail_gb": info.get("mem_avail_gb", 0),
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
    slot: int | None = None
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
        js = await rcon.execute(RCON_HOST, RCON_PORT, RCON_PASSWORD, "status_json")
        tx = await rcon.execute(RCON_HOST, RCON_PORT, RCON_PASSWORD, "status")
    except rcon.RconError as exc:
        return {"players": [], "error": str(exc)}
    roster = merge_roster(js, tx)
    # Only pay for the extra round-trip when something actually needs it.
    if any(not p["bot"] and not p["identified"] for p in roster["players"]):
        extra = await resolve_identities()
        if extra:
            roster = merge_roster(js, tx, extra)
    return roster


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
    else:
        # Prefer the SteamID; fall back to #slot for players sv_lan left
        # unauthenticated. Ban and mute are SteamID-only -- they must outlive
        # the session, and a slot number does not.
        target = sid or (f"#{body.slot}" if body.slot is not None else None)
        if not target:
            raise HTTPException(400, "no SteamID or slot to target")

        if body.action == "kick":
            cmd = f'css_kick {target} "{reason}"'
        elif body.action == "slay":
            cmd = f"css_slay {target}"
        elif body.action == "swap":
            cmd = f"css_swap {target}"
        elif body.action in ("ban", "mute", "unmute"):
            if not sid:
                raise HTTPException(
                    400, f"{body.action} needs a SteamID, and this player has none "
                         "(sv_lan skips Steam auth). Kick or slay them instead.")
            cmd = {"ban": f'css_addban {sid} {body.duration} "{reason}"',
                   "mute": f'css_addgag {sid} {body.duration} "{reason}"',
                   "unmute": f"css_ungag {sid}"}[body.action]
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
    # Optional finish. A knife needs BOTH rows: the model in wp_player_knife and
    # its paint in wp_player_skins. Setting only the model gives the vanilla one.
    weapon_defindex: int | None = None
    paint: int | None = None


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
    sid = _valid_steamid(body.steamid64)
    loadout.set_knife(sid, body.weapon_name)
    finish = None
    if body.paint is not None and body.weapon_defindex:
        loadout.set_skin(sid, body.weapon_defindex, body.paint)
        finish = body.paint
    return {"ok": True, "knife": body.weapon_name, "paint": finish,
            "note": "respawn to see it (!kill)"}


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


# -------------------------------------------------------------------- presets
class PresetBody(BaseModel):
    steamid64: str
    slot: int
    name: str = ""


@app.get("/api/presets/{steamid64}")
async def presets_list(steamid64: str) -> list[dict[str, Any]]:
    return presets.list_for(_valid_steamid(steamid64))


@app.post("/api/presets")
async def presets_save(body: PresetBody) -> dict[str, Any]:
    if not 1 <= body.slot <= 9:
        raise HTTPException(400, "slot must be 1-9")
    return presets.capture(_valid_steamid(body.steamid64), body.slot, body.name)


@app.post("/api/presets/apply")
async def presets_apply(body: PresetBody) -> dict[str, Any]:
    return presets.apply(_valid_steamid(body.steamid64), body.slot)


@app.delete("/api/presets/{steamid64}/{slot}")
async def presets_delete(steamid64: str, slot: int) -> dict[str, Any]:
    return {"ok": True, "removed": presets.delete(_valid_steamid(steamid64), slot)}


@app.get("/api/watcher")
async def watcher_state() -> dict[str, Any]:
    return watcher.snapshot()


# --------------------------------------------------------------- game servers
# Switching games is destructive: it takes down whatever is running, including
# anyone mid-round. So the UI must not be able to do it on a single click. The
# contract is that /start refuses with 409 and names what it would have to stop,
# and only proceeds once the caller sends confirm=true having shown that to a
# human. ui/app/servers.py explains why only one server may run.
class SwitchBody(BaseModel):
    confirm: bool = False


@app.get("/api/servers")
async def servers_list() -> dict[str, Any]:
    rows = await servers.status_all()
    return {"servers": rows, "running": servers.running_ids(rows)}


@app.post("/api/servers/{game}/start")
async def servers_start(game: str, body: SwitchBody) -> dict[str, Any]:
    if game not in servers.GAMES:
        raise HTTPException(404, f"unknown game: {game}")

    rows = await servers.status_all()
    me = next(r for r in rows if r["id"] == game)
    if not me.get("available"):
        raise HTTPException(409, me.get("detail", "that server is not available"))
    if me["state"] in ("running", "starting"):
        return {"ok": True, "started": game, "stopped": [], "note": "already running"}

    others = [g for g in servers.running_ids(rows) if g != game]
    if others and not body.confirm:
        labels = [servers.GAMES[g]["label"] for g in others]
        joined = " and ".join(labels)
        raise HTTPException(409, {
            "needs_confirm": True,
            "game": game,
            "label": servers.GAMES[game]["label"],
            "would_stop": others,
            "would_stop_labels": labels,
            "message": (
                f"Starting {servers.GAMES[game]['label']} will shut down {joined}. "
                f"Anyone connected will be disconnected."
            ),
        })

    try:
        result = await servers.switch_to(game)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, **result}


@app.post("/api/servers/{game}/stop")
async def servers_stop(game: str) -> dict[str, Any]:
    if game not in servers.GAMES:
        raise HTTPException(404, f"unknown game: {game}")
    try:
        await servers.stop(game)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "stopped": game}


@app.get("/api/host")
async def host_health() -> dict[str, Any]:
    return await host.snapshot()


# The LANtern landing page is the root of this service; the CS2 control UI it
# used to serve there now lives at /cs2. Both are plain pages on the same
# origin, so the CS2 UI's own /api/... calls are unaffected by the move.
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "shell.html")


@app.get("/cs2")
async def cs2_ui() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
