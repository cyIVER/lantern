#!/usr/bin/env python3
"""Assemble eggs/lantern-cs2.json from the stock CS2 egg plus src/*.sh.

Keeping the shell in real .sh files (rather than embedded JSON strings) means
they stay editable, diffable and shellcheck-able. Run after editing either script:

    python eggs/build-egg.py
"""
from __future__ import annotations

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).parent
STOCK = HERE / "cs2-stock.json"
OUT = HERE / "lantern-cs2.json"

# Stable UUID so re-importing updates this egg instead of creating duplicates.
LANTERN_UUID = "b5f4c1a2-7e63-4d18-9a2f-3c8d5e0b71aa"

# Verified against game/csgo/maps/*.vpk in an actual install. '_vanity' variants
# are menu backdrops, not playable, and are deliberately excluded.
MAPS = [
    # active duty
    "de_ancient", "de_anubis", "de_dust2", "de_inferno", "de_mirage",
    "de_nuke", "de_overpass", "de_train", "de_vertigo",
    # LAN classics
    "de_cache", "cs_italy", "cs_office", "cs_shelter",
    # other official maps shipped with CS2
    "de_boulder", "de_debris", "de_eldorado", "de_fachwerk", "de_poseidon",
    "ar_baggage", "ar_shoots", "ar_pool_day",
]


def load_scripts() -> str:
    install = (HERE / "src" / "install.sh").read_text(encoding="utf-8")
    embeds = {
        "__BOOT_SCRIPT__": ("boot.sh", "BOOTEOF"),
        "__NORMALIZE_SCRIPT__": ("normalize-plugins.sh", "NORMEOF"),
    }

    for placeholder, (filename, delimiter) in embeds.items():
        if placeholder not in install:
            sys.exit(f"install.sh is missing the {placeholder} placeholder")
        body = (HERE / "src" / filename).read_text(encoding="utf-8")
        # Embedded inside a quoted heredoc, so nothing expands -- but a line
        # equal to the delimiter would terminate it early.
        if any(line.strip() == delimiter for line in body.splitlines()):
            sys.exit(f"{filename} contains a line equal to its heredoc delimiter")
        install = install.replace(placeholder, body.rstrip("\n"))

    return install


def variable(name, env, default, rules, desc, *, user_view=True, user_edit=True):
    return {
        "name": name,
        "description": desc,
        "env_variable": env,
        "default_value": default,
        "user_viewable": user_view,
        "user_editable": user_edit,
        "rules": rules,
        "sort": None,
    }


def main() -> int:
    if not STOCK.exists():
        sys.exit(f"missing {STOCK}; export the stock egg first")
    egg = json.loads(STOCK.read_text(encoding="utf-8"))

    egg["name"] = "LANtern CS2"
    egg["uuid"] = LANTERN_UUID
    egg["author"] = "iveri@lantern.lan"
    egg["description"] = (
        "Counter-Strike 2 for LAN play. Metamod + CounterStrikeSharp with a MODE "
        "switch (competitive / retakes / practice / deathmatch) that activates only "
        "the matching plugin set, because MatchZy, Retakes and PracticeMode conflict "
        "if loaded together. sv_lan 1, no GSLT required."
    )

    egg["startup_commands"] = {"Default": "bash ./lantern/boot.sh"}
    egg["scripts"]["installation"]["script"] = load_scripts()

    # Wings marks a server "running" when this string appears on stdout, and the
    # panel only renders the console for a running server.
    #
    # The stock egg uses "Connection to Steam servers successful", which CS2
    # never prints here -- so Wings stayed in "starting" forever and the console
    # never appeared. "GC Connection established" is emitted once the server has
    # registered with Valve's game coordinator, i.e. genuinely ready for players.
    egg.setdefault("config", {})["startup"] = json.dumps(
        {"done": "GC Connection established"}
    )
    egg["config"]["stop"] = "quit"

    keep = {
        "AUTO_UPDATE", "RCON_ENABLED", "VAC_ENABLED", "SRCDS_MAP", "MAX_PLAYERS",
        "RCON_PASSWORD", "SERVER_NAME", "SERVER_PASSWORD", "SRCDS_APPID", "TV_PORT",
        "STEAM_GSLT",
    }
    kept = [v for v in egg.get("variables", []) if v.get("env_variable") in keep]

    for v in kept:
        env = v["env_variable"]
        if env == "STEAM_GSLT":
            # Stock marks this required|size:32. On a LAN server there is no GSLT,
            # so creation would be impossible. boot.sh omits the flag when empty.
            v["rules"] = ["nullable", "string", "alpha_num", "size:32"]
            v["default_value"] = ""
            v["description"] = (
                "Game Server Login Token. Leave EMPTY for LAN play -- sv_lan 1 does "
                "not need one. Only required to appear in the public server browser."
            )
        elif env == "SRCDS_MAP":
            # An 'in:' rule renders as a dropdown in the panel instead of a
            # free-text box. Every entry is verified present in the CS2 install
            # (game/csgo/maps/*.vpk), so nothing here can fail to load.
            v["rules"] = ["required", "string", "in:" + ",".join(MAPS)]
            v["default_value"] = "de_dust2"
            v["description"] = (
                "Starting map. Active duty pool plus LAN classics. Change it here "
                "and restart, or switch live from the console with 'changelevel <map>'."
            )
        elif env == "MAX_PLAYERS":
            v["default_value"] = "12"
        elif env == "SERVER_NAME":
            v["default_value"] = "LANtern"
        elif env == "TV_PORT":
            v["default_value"] = "27020"

    # GAME_MODE / GAME_TYPE are dropped: boot.sh derives them from MODE, and
    # leaving both would let the two disagree.
    kept.insert(0, variable(
        "Mode", "MODE", "competitive",
        ["required", "string", "in:competitive,retakes,practice,deathmatch"],
        "Which game mode to boot into. Changing this needs a restart. "
        "competitive = MatchZy 5v5 (knife round, .ready, MR12, demos). "
        "retakes = CS2-Retakes. practice = nade lineups, sv_cheats, infinite ammo. "
        "deathmatch = vanilla DM. Only the matching plugin set is loaded.",
    ))
    kept.append(variable(
        "Enable weapon skins", "ENABLE_SKINS", "0", ["required", "boolean"],
        "Load WeaponPaints so everyone can pick knives, gloves and skins. "
        "Requires a MySQL database configured for the plugin -- leave 0 until "
        "you have created one, or the plugin will error on load.",
    ))
    kept.append(variable(
        "Bot quota", "BOT_QUOTA", "10", ["required", "numeric", "between:0,32"],
        "Bots fill empty slots up to this number (bot_quota_mode fill). "
        "They are kicked automatically as humans join.",
    ))
    kept.append(variable(
        "Bot difficulty", "BOT_DIFFICULTY", "2", ["required", "numeric", "between:0,3"],
        "0 = easy, 1 = normal, 2 = hard, 3 = expert.",
    ))

    for i, v in enumerate(kept):
        v["sort"] = i + 1
    egg["variables"] = kept

    OUT.write_text(json.dumps(egg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT.name}  ({OUT.stat().st_size:,} bytes)")
    print(f"  startup:   {egg['startup_commands']['Default']}")
    print(f"  install:   {len(egg['scripts']['installation']['script']):,} chars")
    print(f"  variables: {', '.join(v['env_variable'] for v in kept)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
