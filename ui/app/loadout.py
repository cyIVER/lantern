"""Knife / glove / skin loadout editing, written straight into WeaponPaints' tables.

Catalogue data comes from the plugin's own bundled JSON (skins_en.json and
friends), read read-only from the server volume. That means the list always
matches the WeaponPaints version actually installed -- no separately maintained
item database to drift out of date.

Schema (WeaponPaints creates these itself):
    wp_player_knife   steamid, weapon_team, knife            -- entity name
    wp_player_gloves  steamid, weapon_team, weapon_defindex
    wp_player_skins   steamid, weapon_team, weapon_defindex, weapon_paint_id,
                      weapon_wear, weapon_seed, ...

weapon_team is 2 = T, 3 = CT. Selections are written for both teams so a choice
applies whichever side you end up on.
"""
from __future__ import annotations

import functools
import json
import os
import pathlib
from typing import Any

import pymysql

CATALOG_DIR = pathlib.Path(os.environ.get("CATALOG_DIR", "/volumes"))

DB = dict(
    host=os.environ.get("WP_DB_HOST", "database"),
    port=int(os.environ.get("WP_DB_PORT", "3306")),
    user=os.environ.get("WP_DB_USER", ""),
    password=os.environ.get("WP_DB_PASS", ""),
    database=os.environ.get("WP_DB_NAME", ""),
    charset="utf8mb4",
    autocommit=True,
    connect_timeout=8,
)

TEAMS = (2, 3)  # T, CT

# Knife entity names all start weapon_knife_*, except these.
KNIFE_EXTRA = {"weapon_bayonet"}


def connect():
    return pymysql.connect(cursorclass=pymysql.cursors.DictCursor, **DB)


@functools.lru_cache(maxsize=8)
def _load(name: str) -> list[dict[str, Any]]:
    path = CATALOG_DIR / name
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def _is_knife(entry: dict) -> bool:
    n = entry.get("weapon_name") or ""
    return n.startswith("weapon_knife") or n in KNIFE_EXTRA


def knives() -> list[dict[str, Any]]:
    """One entry per knife model, de-duplicated to the default paint."""
    seen: dict[str, dict] = {}
    for e in _load("skins_en.json"):
        if not _is_knife(e):
            continue
        name = e["weapon_name"]
        seen.setdefault(name, {
            "weapon_name": name,
            "weapon_defindex": e.get("weapon_defindex"),
            "label": (e.get("paint_name") or name).split("|")[0].strip().lstrip("★ ").strip(),
            "image": e.get("image", ""),
        })
    return sorted(seen.values(), key=lambda x: x["label"])


def knife_paints(weapon_name: str) -> list[dict[str, Any]]:
    return [
        {"paint": int(e["paint"]), "paint_name": e.get("paint_name", ""),
         "image": e.get("image", ""), "weapon_defindex": e.get("weapon_defindex")}
        for e in _load("skins_en.json")
        if e.get("weapon_name") == weapon_name and str(e.get("paint", "")).isdigit()
    ]


def weapons() -> list[dict[str, Any]]:
    """Non-knife weapons that have at least one skin."""
    seen: dict[str, dict] = {}
    for e in _load("skins_en.json"):
        if _is_knife(e):
            continue
        name = e.get("weapon_name")
        if not name:
            continue
        seen.setdefault(name, {
            "weapon_name": name,
            "weapon_defindex": e.get("weapon_defindex"),
            "label": name.replace("weapon_", "").replace("_", " ").upper(),
        })
    return sorted(seen.values(), key=lambda x: x["label"])


def skins_for(weapon_name: str) -> list[dict[str, Any]]:
    out = []
    for e in _load("skins_en.json"):
        if e.get("weapon_name") != weapon_name:
            continue
        paint = e.get("paint")
        if not str(paint).isdigit():
            continue
        out.append({
            "paint": int(paint),
            "paint_name": e.get("paint_name", ""),
            "image": e.get("image", ""),
            "weapon_defindex": e.get("weapon_defindex"),
        })
    return out


def gloves() -> list[dict[str, Any]]:
    out = []
    for e in _load("gloves_en.json"):
        paint = e.get("paint")
        if not str(paint).isdigit():
            continue
        out.append({
            "weapon_defindex": e.get("weapon_defindex"),
            "paint": int(paint),
            "paint_name": (e.get("paint_name") or "").lstrip("★ ").strip(),
            "image": e.get("image", ""),
        })
    return sorted(out, key=lambda x: x["paint_name"])


# --------------------------------------------------------------------- writes
def set_knife(steamid: str, weapon_name: str) -> None:
    with connect() as c, c.cursor() as cur:
        for team in TEAMS:
            cur.execute(
                "REPLACE INTO wp_player_knife (steamid, weapon_team, knife) VALUES (%s,%s,%s)",
                (steamid, team, weapon_name))


def set_gloves(steamid: str, defindex: int, paint: int) -> None:
    """Gloves need two rows: the model in wp_player_gloves, the paint in wp_player_skins."""
    with connect() as c, c.cursor() as cur:
        for team in TEAMS:
            cur.execute(
                "REPLACE INTO wp_player_gloves (steamid, weapon_team, weapon_defindex) "
                "VALUES (%s,%s,%s)", (steamid, team, defindex))
            cur.execute(
                "REPLACE INTO wp_player_skins "
                "(steamid, weapon_team, weapon_defindex, weapon_paint_id, weapon_wear, weapon_seed) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (steamid, team, defindex, paint, 0.0, 0))


def set_skin(steamid: str, defindex: int, paint: int,
             wear: float = 0.0, seed: int = 0, stattrak: bool = False) -> None:
    with connect() as c, c.cursor() as cur:
        for team in TEAMS:
            cur.execute(
                "REPLACE INTO wp_player_skins "
                "(steamid, weapon_team, weapon_defindex, weapon_paint_id, weapon_wear, "
                " weapon_seed, weapon_stattrak) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (steamid, team, defindex, paint, wear, seed, 1 if stattrak else 0))


def clear(steamid: str) -> dict[str, int]:
    removed = {}
    with connect() as c, c.cursor() as cur:
        for table in ("wp_player_knife", "wp_player_gloves", "wp_player_skins"):
            removed[table] = cur.execute(f"DELETE FROM {table} WHERE steamid = %s", (steamid,))
    return removed


def current(steamid: str) -> dict[str, Any]:
    with connect() as c, c.cursor() as cur:
        cur.execute("SELECT knife FROM wp_player_knife WHERE steamid=%s LIMIT 1", (steamid,))
        knife = (cur.fetchone() or {}).get("knife")
        cur.execute("SELECT weapon_defindex FROM wp_player_gloves WHERE steamid=%s LIMIT 1", (steamid,))
        glove = (cur.fetchone() or {}).get("weapon_defindex")
        cur.execute(
            "SELECT weapon_defindex, weapon_paint_id FROM wp_player_skins "
            "WHERE steamid=%s AND weapon_team=3", (steamid,))
        skins = {r["weapon_defindex"]: r["weapon_paint_id"] for r in cur.fetchall()}
    return {"knife": knife, "gloves": glove, "skins": skins}


def health() -> dict[str, Any]:
    cat = {n: len(_load(n)) for n in ("skins_en.json", "gloves_en.json")}
    try:
        with connect() as c, c.cursor() as cur:
            cur.execute("SELECT 1")
        db_ok, err = True, None
    except Exception as exc:
        db_ok, err = False, str(exc)
    return {"catalog_dir": str(CATALOG_DIR), "catalog": cat, "db": db_ok, "db_error": err}
