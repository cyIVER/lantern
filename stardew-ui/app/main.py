"""LANtern Stardew control -- backend.

A separate application from the CS2 control UI on purpose. Different game,
different audience, and nothing about a farm belongs behind a tab sitting next
to "Match" and "Knife Round".

Three sources of truth, and they are genuinely different things:

  * JunimoServer's HTTP API  -> live game state: players, cabins, invite code,
    time, rendering. A real REST API, so this is a thin proxy, not a parser.
  * the mods directory       -> what SMAPI will load next start. Enabling and
    disabling is a folder rename; see mods.py.
  * the Docker socket        -> restarting the server, which is the only way a
    mod change can take effect.

That last one is a real privilege: anything that can reach this service can
reach the Docker daemon, which is root-equivalent on the host. It is here
because a mod toggle you cannot apply is not worth shipping. Keep the port on
the LAN. See docs/STARDEW.md.
"""
from __future__ import annotations

import contextlib
import os
import pathlib
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import mods, stardew

STATIC = pathlib.Path(__file__).parent.parent / "static"
SERVER_CONTAINER = os.environ.get("SDV_CONTAINER", "sdvd-server")

app = FastAPI(title="LANtern Stardew Control", docs_url=None, redoc_url=None)


def _err(exc: Exception, code: int = 503) -> HTTPException:
    return HTTPException(code, str(exc))


# --------------------------------------------------------------- game state
@app.get("/api/overview")
async def overview() -> dict[str, Any]:
    """Everything the dashboard needs in one call. Never raises: a farm still
    loading answers /health long before /status, and a partial dashboard is far
    more useful than one error message."""
    if not stardew.configured():
        return {"configured": False,
                "note": "STARDEW_API_URL is not set for this service"}
    data = await stardew.overview()
    with contextlib.suppress(Exception):
        data["mods"] = mods.list_mods()
        data["mod_problems"] = mods.missing_dependencies()
    return data


@app.get("/api/screenshot")
async def screenshot() -> Response:
    try:
        png = await stardew.screenshot()
    except stardew.StardewError as exc:
        raise _err(exc) from exc
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


class TimeBody(BaseModel):
    value: int


class FpsBody(BaseModel):
    fps: int


class NameBody(BaseModel):
    name: str


@app.post("/api/time")
async def set_time(body: TimeBody) -> dict[str, Any]:
    # Stardew's clock runs 600 (6am) to 2600 (2am the following day).
    if not 600 <= body.value <= 2600:
        raise HTTPException(400, "time must be between 600 (6am) and 2600 (2am)")
    try:
        return {"ok": True, "result": await stardew.set_time(body.value)}
    except stardew.StardewError as exc:
        raise _err(exc) from exc


@app.post("/api/rendering")
async def set_rendering(body: FpsBody) -> dict[str, Any]:
    if not 0 <= body.fps <= 60:
        raise HTTPException(400, "fps must be 0-60 (0 disables rendering)")
    try:
        return {"ok": True, "result": await stardew.set_rendering(body.fps)}
    except stardew.StardewError as exc:
        raise _err(exc) from exc


@app.post("/api/reload")
async def reload_world() -> dict[str, Any]:
    try:
        return {"ok": True, "result": await stardew.reload()}
    except stardew.StardewError as exc:
        raise _err(exc) from exc


@app.post("/api/admin")
async def grant_admin(body: NameBody) -> dict[str, Any]:
    try:
        return {"ok": True, "result": await stardew.grant_admin(body.name)}
    except stardew.StardewError as exc:
        raise _err(exc) from exc


# ---------------------------------------------------------------------- mods
class ToggleBody(BaseModel):
    folder: str
    enabled: bool


@app.get("/api/mods")
async def list_mods() -> dict[str, Any]:
    data = mods.list_mods()
    data["problems"] = mods.missing_dependencies()
    return data


@app.post("/api/mods/toggle")
async def toggle_mod(body: ToggleBody) -> dict[str, Any]:
    try:
        return mods.set_enabled(body.folder, body.enabled)
    except mods.ModError as exc:
        raise _err(exc, 400) from exc


# ------------------------------------------------------------------- restart
@app.post("/api/restart")
async def restart_server() -> dict[str, Any]:
    """Restart the game container. The only way a mod toggle takes effect."""
    try:
        import docker
    except ImportError as exc:                       # pragma: no cover
        raise _err(RuntimeError("the docker library is not installed"), 500) from exc

    try:
        client = docker.from_env()
        c = client.containers.get(SERVER_CONTAINER)
        c.restart(timeout=30)
    except Exception as exc:                          # noqa: BLE001
        raise _err(RuntimeError(f"could not restart {SERVER_CONTAINER}: {exc}")) from exc

    return {"ok": True, "container": SERVER_CONTAINER,
            "note": "the farm takes a minute or two to load"}


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "api": stardew.BASE or None, "mods_dir": str(mods.MODS_DIR)}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
