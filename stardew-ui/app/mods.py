"""SMAPI mod inventory and enable/disable, over the mods directory.

SMAPI has no concept of a disabled mod. The community convention -- and what
SMAPI itself documents -- is that a folder whose name starts with a dot is
skipped entirely during loading. So "disable" here is a rename:

    mods/Automate      <->  mods/.Automate

That is honest about what is happening and survives anything: no state file to
desynchronise, no database, and a human poking around in the folder by hand
sees exactly the same truth the UI does.

The catch, and it is unavoidable: SMAPI enumerates mods once at process start.
A toggle therefore does nothing until the server restarts, so every response
carries `restart_required` and the UI says so plainly rather than pretending
the change took effect.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
from typing import Any

MODS_DIR = pathlib.Path(os.environ.get("MODS_DIR", "/mods"))

# Folder names are used to build paths, so refuse anything that could escape.
SAFE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._ ()+-]{0,120}$")


class ModError(RuntimeError):
    pass


def _safe(folder: str) -> str:
    if not SAFE.match(folder) or "/" in folder or "\\" in folder or ".." in folder:
        raise ModError(f"unsafe folder name: {folder!r}")
    return folder


def _read_manifest(d: pathlib.Path) -> dict[str, Any]:
    """SMAPI manifests are JSON, but hand-edited ones routinely carry trailing
    commas and BOMs. Recover what we can rather than dropping the mod."""
    f = d / "manifest.json"
    if not f.is_file():
        # Some mods nest one level down (a zip extracted with its wrapper).
        nested = next((c / "manifest.json" for c in d.iterdir()
                       if c.is_dir() and (c / "manifest.json").is_file()), None)
        if nested is None:
            return {}
        f = nested
    try:
        raw = f.read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        cleaned = re.sub(r",(\s*[}\]])", r"\1", raw)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return {"_unparsable": True}


def list_mods() -> dict[str, Any]:
    if not MODS_DIR.is_dir():
        return {"ok": False, "error": f"{MODS_DIR} is not mounted", "mods": []}

    out: list[dict[str, Any]] = []
    for d in sorted(MODS_DIR.iterdir(), key=lambda x: x.name.lstrip(".").lower()):
        if not d.is_dir():
            continue
        disabled = d.name.startswith(".")
        m = _read_manifest(d)
        out.append({
            "folder": d.name,
            "name": m.get("Name") or d.name.lstrip("."),
            "author": m.get("Author") or "",
            "version": m.get("Version") or "",
            "description": (m.get("Description") or "")[:240],
            "unique_id": m.get("UniqueID") or "",
            "enabled": not disabled,
            # A content pack ships assets for another mod rather than code.
            "content_pack_for": (m.get("ContentPackFor") or {}).get("UniqueID", ""),
            "dependencies": [
                dep.get("UniqueID", "") for dep in (m.get("Dependencies") or [])
                if isinstance(dep, dict)
            ],
            "unparsable": bool(m.get("_unparsable")),
            "no_manifest": not m,
        })

    return {"ok": True, "dir": str(MODS_DIR), "mods": out,
            "enabled": sum(1 for m in out if m["enabled"]),
            "disabled": sum(1 for m in out if not m["enabled"])}


def set_enabled(folder: str, enabled: bool) -> dict[str, Any]:
    _safe(folder)
    src = MODS_DIR / folder
    if not src.is_dir():
        raise ModError(f"no such mod folder: {folder}")

    bare = folder.lstrip(".")
    target = MODS_DIR / (bare if enabled else "." + bare)

    if src == target:
        return {"ok": True, "folder": folder, "enabled": enabled,
                "changed": False, "restart_required": False}

    if target.exists():
        raise ModError(f"{target.name} already exists; resolve it by hand")

    try:
        src.rename(target)
    except OSError as exc:
        raise ModError(f"could not rename: {exc}") from exc

    return {"ok": True, "folder": target.name, "enabled": enabled,
            "changed": True,
            # SMAPI enumerates mods once, at process start.
            "restart_required": True}


def missing_dependencies() -> list[dict[str, str]]:
    """Enabled mods whose dependencies are absent or disabled.

    SMAPI reports these itself, but only in a log nobody reads until something
    is already broken -- and a mod disabled by accident takes its dependents
    down silently.
    """
    data = list_mods()
    if not data.get("ok"):
        return []
    live = {m["unique_id"].lower() for m in data["mods"] if m["enabled"] and m["unique_id"]}
    problems = []
    for m in data["mods"]:
        if not m["enabled"]:
            continue
        for dep in m["dependencies"]:
            if dep and dep.lower() not in live:
                problems.append({"mod": m["name"], "missing": dep})
        cp = m["content_pack_for"]
        if cp and cp.lower() not in live:
            problems.append({"mod": m["name"], "missing": cp})
    return problems
