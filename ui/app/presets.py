"""Loadout presets, stored alongside WeaponPaints' own tables.

A preset is a complete loadout -- knife (with finish), gloves, and any number of
weapon skins -- saved under a slot number 1-9 so it can be recalled in chat with
`!1` .. `!9`.

Kept in its own table rather than WeaponPaints' so an upgrade of the plugin
cannot drop it, and so applying a preset is a plain copy into the plugin's
tables, which means WeaponPaints needs no awareness of any of this.
"""
from __future__ import annotations

import json
from typing import Any

from . import loadout

SCHEMA = """
CREATE TABLE IF NOT EXISTS lantern_presets (
    steamid  VARCHAR(18)  NOT NULL,
    slot     TINYINT      NOT NULL,
    name     VARCHAR(64)  NOT NULL DEFAULT '',
    payload  JSON         NOT NULL,
    updated  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (steamid, slot)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def ensure_schema() -> None:
    """Create the lantern_presets table if it does not already exist."""
    with loadout.connect() as c, c.cursor() as cur:
        cur.execute(SCHEMA)


def list_for(steamid: str) -> list[dict[str, Any]]:
    """Retrieve all saved loadout presets for a player."""
    with loadout.connect() as c, c.cursor() as cur:
        cur.execute(
            "SELECT slot, name, payload, updated FROM lantern_presets "
            "WHERE steamid=%s ORDER BY slot", (steamid,))
        rows = cur.fetchall()
    out = []
    for r in rows:
        payload = r["payload"]
        if isinstance(payload, (str, bytes)):
            payload = json.loads(payload)
        out.append({
            "slot": r["slot"],
            "name": r["name"],
            "updated": str(r["updated"]),
            "knife": payload.get("knife"),
            "gloves": payload.get("gloves"),
            "skins": payload.get("skins", {}),
            "count": len(payload.get("skins", {})),
        })
    return out


def capture(steamid: str, slot: int, name: str = "") -> dict[str, Any]:
    """Snapshot the player's CURRENT loadout into a preset slot."""
    cur_state = loadout.current(steamid)
    payload = {
        "knife": cur_state.get("knife"),
        "knife_paint": None,
        "gloves": cur_state.get("gloves"),
        "skins": {str(k): v for k, v in (cur_state.get("skins") or {}).items()},
    }
    # The knife's own finish lives in wp_player_skins under its defindex, so pull
    # it back out and store it separately -- restoring needs both halves.
    if payload["knife"]:
        for entry in loadout.knives():
            if entry["weapon_name"] == payload["knife"]:
                di = str(entry.get("weapon_defindex"))
                if di in payload["skins"]:
                    payload["knife_paint"] = payload["skins"][di]
                break

    with loadout.connect() as c, c.cursor() as cur:
        cur.execute(
            "REPLACE INTO lantern_presets (steamid, slot, name, payload) VALUES (%s,%s,%s,%s)",
            (steamid, slot, name or f"Preset {slot}", json.dumps(payload)))
    return {"slot": slot, "name": name or f"Preset {slot}", **payload}


def delete(steamid: str, slot: int) -> int:
    """Delete a specific preset slot for a player and return the count of rows removed."""
    with loadout.connect() as c, c.cursor() as cur:
        return cur.execute(
            "DELETE FROM lantern_presets WHERE steamid=%s AND slot=%s", (steamid, slot))


def apply(steamid: str, slot: int) -> dict[str, Any]:
    """Write a saved preset back into WeaponPaints' tables."""
    with loadout.connect() as c, c.cursor() as cur:
        cur.execute(
            "SELECT name, payload FROM lantern_presets WHERE steamid=%s AND slot=%s",
            (steamid, slot))
        row = cur.fetchone()
    if not row:
        return {"ok": False, "error": f"no preset in slot {slot}"}

    payload = row["payload"]
    if isinstance(payload, (str, bytes)):
        payload = json.loads(payload)

    # Start from a clean slate so a preset with fewer skins does not leave
    # leftovers from whatever was set before.
    loadout.clear(steamid)

    if payload.get("knife"):
        loadout.set_knife(steamid, payload["knife"])
    for defindex, paint in (payload.get("skins") or {}).items():
        loadout.set_skin(steamid, int(defindex), int(paint))
    if payload.get("gloves"):
        gl = int(payload["gloves"])
        paint = (payload.get("skins") or {}).get(str(gl))
        loadout.set_gloves(steamid, gl, int(paint) if paint is not None else 0)

    return {"ok": True, "slot": slot, "name": row["name"],
            "skins": len(payload.get("skins") or {})}
