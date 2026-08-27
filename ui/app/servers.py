"""Start and stop game servers, one at a time.

WHY ONE AT A TIME

The VM has ~17 GB usable. CS2 is allocated 8 GB and ATM10 11 GB, and Stardew
runs a real copy of the game under a virtual display. Any two of them together
overcommit the box, and the failure mode is not a tidy error -- the kernel OOM
killer takes whichever process it likes, usually mid-save. So the rule is
enforced here rather than left to the operator to remember.

This mirrors what the `lantern` script does on the VM. The two are deliberately
separate: the script is what you reach for over SSH, this is what the control UI
calls, and neither should depend on the other being installed.

TWO KINDS OF SERVER

CS2 and Minecraft are Pelican servers -- Wings owns their containers, so they
are driven through the panel's client API. Their UUIDs are discovered by name at
runtime rather than configured, because a UUID in a .env file is one more thing
to get wrong after a rebuild, and the panel already knows the answer.

Stardew is not a Pelican server at all. It is a separate compose project, so it
is driven directly through the Docker socket. That means the UI container needs
the socket mounted; without it Stardew shows as unavailable rather than
breaking the rest of the page.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx

PELICAN_URL = os.environ.get("PELICAN_URL", "http://panel").rstrip("/")
API_KEY = os.environ.get("PELICAN_API_KEY", "")
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")

# Order matters: it is the order the UI lists them in.
GAMES: dict[str, dict[str, Any]] = {
    "cs2": {
        "label": "Counter-Strike 2",
        "kind": "pelican",
        "panel_name": "LANtern CS2",
        "note": "8 GB",
    },
    "minecraft": {
        "label": "Minecraft (All the Mods 10)",
        "kind": "pelican",
        "panel_name": "LANtern Minecraft",
        "note": "11 GB",
    },
    "stardew": {
        "label": "Stardew Valley",
        "kind": "docker",
        # steam-auth first: the server waits on it for a Steam session.
        "containers": ["sdvd-steam-auth", "sdvd-server"],
        # sdvd-ui is the Stardew control UI. It is started with the game but
        # never stopped with it: the LANtern landing page links to it, and a
        # management UI that disappears whenever the thing it manages is off is
        # a management UI you cannot use to turn that thing on.
        "keep_up": ["sdvd-ui"],
        "note": "separate compose project",
    },
}

# Panel name -> uuid. Cached because it cannot change without a server being
# recreated, and the lookup is a round trip on every poll otherwise.
_uuids: dict[str, str] = {}


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


# ------------------------------------------------------------------ pelican
async def _panel_servers() -> dict[str, str]:
    """Map panel server name -> uuid."""
    if _uuids:
        return _uuids
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{PELICAN_URL}/api/client", headers=_headers())
    r.raise_for_status()
    for item in r.json().get("data", []):
        attrs = item.get("attributes", {})
        if attrs.get("name") and attrs.get("uuid"):
            _uuids[attrs["name"]] = attrs["uuid"]
    return _uuids


async def _pelican_state(uuid: str) -> str:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{PELICAN_URL}/api/client/servers/{uuid}/resources",
                             headers=_headers())
    if r.status_code >= 400:
        return "unknown"
    return r.json().get("attributes", {}).get("current_state", "unknown")


async def _pelican_power(uuid: str, signal: str) -> None:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(f"{PELICAN_URL}/api/client/servers/{uuid}/power",
                              headers=_headers(), json={"signal": signal})
    if r.status_code >= 400:
        raise RuntimeError(f"panel refused {signal}: {r.text[:200]}")


# ------------------------------------------------------------------- docker
def _docker() -> httpx.AsyncClient:
    transport = httpx.AsyncHTTPTransport(uds=DOCKER_SOCK)
    return httpx.AsyncClient(transport=transport, base_url="http://docker", timeout=30)


async def _docker_available() -> bool:
    try:
        async with _docker() as c:
            r = await c.get("/_ping")
        return r.status_code == 200
    except Exception:
        return False


async def _container_state(name: str) -> str:
    """running / exited / absent."""
    try:
        async with _docker() as c:
            r = await c.get(f"/containers/{name}/json")
        if r.status_code == 404:
            return "absent"
        if r.status_code >= 400:
            return "unknown"
        return r.json().get("State", {}).get("Status", "unknown")
    except Exception:
        return "unknown"


async def _container_power(name: str, action: str) -> None:
    """action is 'start' or 'stop'. Already-in-that-state is not an error."""
    async with _docker() as c:
        r = await c.post(f"/containers/{name}/{action}")
    # 304 means it was already started/stopped, which is exactly what we wanted.
    if r.status_code not in (204, 304, 404):
        raise RuntimeError(f"docker {action} {name}: {r.status_code} {r.text[:160]}")


# -------------------------------------------------------------------- state
def _normalise(state: str) -> str:
    """Collapse the two backends' vocabularies into one the UI can render."""
    if state in ("running",):
        return "running"
    if state in ("starting", "restarting", "created"):
        return "starting"
    if state in ("stopping", "removing", "paused"):
        return "stopping"
    if state in ("offline", "exited", "dead"):
        return "stopped"
    if state == "absent":
        return "absent"
    return "unknown"


async def status(game: str) -> dict[str, Any]:
    spec = GAMES[game]
    if spec["kind"] == "pelican":
        try:
            uuids = await _panel_servers()
            uuid = uuids.get(spec["panel_name"])
        except Exception as exc:
            return {"id": game, "label": spec["label"], "note": spec["note"],
                    "state": "unknown", "available": False,
                    "detail": f"panel unreachable: {exc}"}
        if not uuid:
            return {"id": game, "label": spec["label"], "note": spec["note"],
                    "state": "absent", "available": False,
                    "detail": f"no server named {spec['panel_name']!r} in the panel"}
        raw = await _pelican_state(uuid)
        return {"id": game, "label": spec["label"], "note": spec["note"],
                "state": _normalise(raw), "raw": raw, "available": True, "uuid": uuid}

    # docker-backed
    if not await _docker_available():
        return {"id": game, "label": spec["label"], "note": spec["note"],
                "state": "unknown", "available": False,
                "detail": "the Docker socket is not mounted into this container"}

    states = await asyncio.gather(*(_container_state(n) for n in spec["containers"]))
    main = states[spec["containers"].index("sdvd-server")]

    # Whether the game's own control UI is reachable is a separate question
    # from whether the game is running, and the landing page links to it. A
    # link that silently goes nowhere is worse than one that says why.
    keep = spec.get("keep_up", [])
    keep_states = await asyncio.gather(*(_container_state(n) for n in keep)) if keep else []
    ui_up = all(st == "running" for st in keep_states) if keep_states else True

    if "absent" in states:
        return {"id": game, "label": spec["label"], "note": spec["note"],
                "state": "absent", "available": False, "ui_up": ui_up,
                "detail": "containers not created yet -- run `lantern use stardew` on the VM once"}
    return {"id": game, "label": spec["label"], "note": spec["note"],
            "state": _normalise(main), "raw": main, "available": True,
            "ui_up": ui_up}


async def status_all() -> list[dict[str, Any]]:
    return list(await asyncio.gather(*(status(g) for g in GAMES)))


def running_ids(rows: list[dict[str, Any]]) -> list[str]:
    return [r["id"] for r in rows if r["state"] in ("running", "starting")]


# ------------------------------------------------------------------ actions
async def stop(game: str) -> None:
    spec = GAMES[game]
    if spec["kind"] == "pelican":
        uuids = await _panel_servers()
        uuid = uuids.get(spec["panel_name"])
        if uuid:
            await _pelican_power(uuid, "stop")
        return
    # Reverse order: the server should go down before the thing it authenticates
    # against, or it spends its last seconds logging connection errors.
    for name in reversed(spec["containers"]):
        await _container_power(name, "stop")


async def wait_stopped(game: str, timeout: float = 90.0) -> bool:
    """Poll until the game is actually down. A start issued while the previous
    server is still releasing its memory is how the box gets overcommitted."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        row = await status(game)
        if row["state"] in ("stopped", "absent"):
            return True
        await asyncio.sleep(2)
    return False


async def start(game: str) -> None:
    spec = GAMES[game]
    if spec["kind"] == "pelican":
        uuids = await _panel_servers()
        uuid = uuids.get(spec["panel_name"])
        if not uuid:
            raise RuntimeError(f"no server named {spec['panel_name']!r} in the panel")
        await _pelican_power(uuid, "start")
        return
    for name in spec["containers"] + spec.get("keep_up", []):
        await _container_power(name, "start")


async def switch_to(game: str) -> dict[str, Any]:
    """Stop everything else, wait for it, then start `game`."""
    rows = await status_all()
    others = [g for g in running_ids(rows) if g != game]

    stopped, stubborn = [], []
    for other in others:
        await stop(other)
        (stopped if await wait_stopped(other) else stubborn).append(other)

    if stubborn:
        # Refuse rather than start anyway. Two servers up is the state this
        # whole module exists to prevent.
        raise RuntimeError(
            "these did not stop in time, so nothing was started: " + ", ".join(stubborn)
        )

    await start(game)
    return {"started": game, "stopped": stopped}
