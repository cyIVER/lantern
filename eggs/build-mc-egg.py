#!/usr/bin/env python3
"""Assemble eggs/lantern-minecraft.json from src/mc-*.sh.

Unlike the CS2 egg there is no stock egg to derive from -- the Pelican modpack
eggs all drive CurseForge through a manifest, which cannot install All the Mods
unattended (seven of its 485 mods block third-party downloads). This builds the
egg from scratch around the *server* pack instead.

Run after editing either script:

    python eggs/build-mc-egg.py
"""
from __future__ import annotations

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).parent
OUT = HERE / "lantern-minecraft.json"

# Stable UUID so re-importing updates this egg instead of creating a duplicate.
LANTERN_MC_UUID = "d3a71b60-5c92-4e08-b1f7-6a2e9c40d5b3"

# All the Mods 10, pinned. Both values are verified against the CurseForge API
# by tools/check-pack-update.py, which opens a PR when a newer release lands
# rather than moving the server underneath everyone.
ATM10_PROJECT = 925200
ATM10_FILE = 8649077  # "All the Mods 10-8.0", NeoForge 21.1.247 / MC 1.21.1

DIFFICULTIES = ["peaceful", "easy", "normal", "hard"]


def load_scripts() -> str:
    install = (HERE / "src" / "mc-install.sh").read_text(encoding="utf-8")
    placeholder, filename, delimiter = "__MC_BOOT_SCRIPT__", "mc-boot.sh", "BOOTEOF"

    if placeholder not in install:
        sys.exit(f"mc-install.sh is missing the {placeholder} placeholder")

    body = (HERE / "src" / filename).read_text(encoding="utf-8")
    # Embedded in a quoted heredoc so nothing expands -- but a line equal to the
    # delimiter would end it early and truncate the script silently.
    if any(line.strip() == delimiter for line in body.splitlines()):
        sys.exit(f"{filename} contains a line equal to its heredoc delimiter")

    return install.replace(placeholder, body.rstrip("\n"))


def variable(name, env, default, rules, desc, *, view=True, edit=True):
    return {
        "name": name,
        "description": desc,
        "env_variable": env,
        "default_value": str(default),
        "user_viewable": view,
        "user_editable": edit,
        "rules": rules,
        "sort": None,
    }


VARIABLES = [
    variable(
        "CurseForge project", "CF_PROJECT_ID", ATM10_PROJECT,
        ["required", "numeric"],
        "CurseForge modpack project id. 925200 is All the Mods 10. The pack must "
        "publish a server pack -- client-only packs cannot install unattended.",
        edit=False,
    ),
    variable(
        "Pack version (file id)", "CF_FILE_ID", ATM10_FILE,
        ["required", "string", "max:20"],
        "Exact CurseForge file id to install -- this is the version pin. "
        "'latest' tracks the newest release, which is convenient for a first "
        "install and a bad idea afterwards: the server would move to a version "
        "your friends do not have and lock every one of them out. Changing this "
        "requires a reinstall, not a restart.",
    ),
    variable(
        "CurseForge API key", "CURSEFORGE_API_KEY", "",
        ["required", "string", "max:120"],
        "From console.curseforge.com. Used only at install time to resolve and "
        "download the server pack. See docs/SECRETS.md.",
        view=False,
    ),
    variable(
        "Max players", "MAX_PLAYERS", 8, ["required", "numeric", "between:1,64"],
        "Player slots. Sizing note: this pack wants 10 GB of heap for 2-5 "
        "players and 12-14 GB for more.",
    ),
    variable(
        "MOTD", "MOTD", "LANtern", ["required", "string", "max:59"],
        "Shown in the multiplayer server list.",
    ),
    variable(
        "Difficulty", "DIFFICULTY", "normal",
        ["required", "string", "in:" + ",".join(DIFFICULTIES)],
        "Applied on every start.",
    ),
    variable(
        "View distance", "VIEW_DISTANCE", 8, ["required", "numeric", "between:3,32"],
        "Chunks sent to clients. Along with simulation distance this is the "
        "biggest lever on server memory -- 8 is comfortable for a modded pack at "
        "eight players, 12 is where a 32 GB host starts to hurt.",
    ),
    variable(
        "Simulation distance", "SIMULATION_DISTANCE", 6,
        ["required", "numeric", "between:3,32"],
        "Chunks that actually tick. Cheaper to lower than view distance and "
        "less noticeable.",
    ),
    variable(
        "Online mode", "ONLINE_MODE", "1", ["required", "boolean"],
        "Verify players against Mojang. Keep this on -- everyone has a real "
        "Java account, and turning it off lets anyone join as anyone.",
    ),
    variable(
        "PvP", "PVP", "1", ["required", "boolean"], "Allow players to damage each other.",
    ),
    variable(
        "Whitelist", "WHITELIST", "0", ["required", "boolean"],
        "Restrict joining to whitelisted accounts. Off is fine on a LAN; turn it "
        "on if you ever forward the port.",
    ),
    variable(
        "RCON password", "RCON_PASSWORD", "", ["nullable", "string", "max:64"],
        "Enables RCON so the LANtern control UI can read the roster and run "
        "commands. Leave empty to disable RCON entirely.",
        view=False,
    ),
    variable(
        "RCON port", "RCON_PORT", 25575, ["required", "numeric", "between:1024,65535"],
        "Only used when an RCON password is set.",
    ),
    variable(
        "JVM headroom (MiB)", "JVM_HEADROOM_MB", 1024,
        ["required", "numeric", "between:512,4096"],
        "Memory held back from the heap for metaspace, GC structures and direct "
        "buffers. Too low and the kernel OOM-kills the container with no Java "
        "stack trace. Raise it if you see kills at a heap that looks healthy.",
    ),
]


def main() -> int:
    egg = {
        "_comment": "LANtern Minecraft egg -- generated by eggs/build-mc-egg.py, do not edit by hand",
        "meta": {"version": "PLCN_v3", "update_url": None},
        "exported_at": "2026-08-24T00:00:00+00:00",
        "name": "LANtern Minecraft",
        "author": "iveri@lantern.lan",
        "uuid": LANTERN_MC_UUID,
        "description": (
            "CurseForge modpack server, pinned to an exact file id. Installs the "
            "author's server pack (every jar included) rather than resolving a "
            "client manifest, because packs like All the Mods 10 contain mods "
            "that block third-party downloads and cannot install unattended. "
            "Defaults to All the Mods 10 on NeoForge / Minecraft 1.21.1, Java 21."
        ),
        "tags": ["minecraft", "modded", "neoforge", "curseforge"],
        "features": ["eula", "java_version", "pid_limit"],
        "docker_images": {
            "Java 21": "ghcr.io/pelican-eggs/yolks:java_21",
            "Java 25": "ghcr.io/pelican-eggs/yolks:java_25",
        },
        "file_denylist": [],
        "startup_commands": {"Default": "bash ./lantern/boot.sh"},
        "config": {
            "files": "{}",
            # Minecraft prints: Done (41.283s)! For help, type "help"
            # Wings will not mark the server running -- and the panel will not
            # render its console -- until it sees this.
            "startup": json.dumps({"done": ")! For help, type "}),
            "logs": "{}",
            "stop": "stop",
        },
        "scripts": {
            "installation": {
                "script": load_scripts(),
                # Needs a JDK, because the server pack ships no libraries/
                # and the loader's installer has to run here to generate them.
                #
                # NOT a yolks image. Those are runtime images that drop to the
                # unprivileged `container` user, which cannot read the install
                # script Wings writes (mode 0600, root) -- the container dies
                # with "Permission denied" in under a second, and Wings reports
                # the install as completed anyway. eclipse-temurin runs as root
                # and has apt, so the script can install its own tools.
                "container": "eclipse-temurin:21-jdk",
                "entrypoint": "/bin/bash",
            }
        },
        "variables": [dict(v, sort=i + 1) for i, v in enumerate(VARIABLES)],
    }

    OUT.write_text(json.dumps(egg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT.name}  ({OUT.stat().st_size:,} bytes)")
    print(f"  startup:   {egg['startup_commands']['Default']}")
    print(f"  done-str:  {json.loads(egg['config']['startup'])['done']!r}")
    print(f"  install:   {len(egg['scripts']['installation']['script']):,} chars")
    print(f"  pinned:    project {ATM10_PROJECT} file {ATM10_FILE}")
    print(f"  variables: {', '.join(v['env_variable'] for v in VARIABLES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
