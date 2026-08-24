#!/usr/bin/env python3
"""Compare the pinned modpack version against CurseForge's latest release.

Deliberately does NOT touch a running server. A modpack upgrade is a breaking
change for every player at once -- the server and every client must be on the
identical version, and there is no mechanism to update your friends' machines.
An unattended upgrade does not save coordination, it forces it at 3 AM with no
warning. So this only ever reports, or edits the pin in a branch for a human to
merge.

    python tools/check-pack-update.py                 # report; exit 0 if current, 10 if behind
    python tools/check-pack-update.py --write         # also move the pin and rebuild the egg
    python tools/check-pack-update.py --github-output # emit key=value for GITHUB_OUTPUT

Needs CURSEFORGE_API_KEY in the environment.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
BUILDER = REPO / "eggs" / "build-mc-egg.py"
API = "https://api.curseforge.com/v1"

RELEASE = 1  # CurseForge releaseType: 1=release, 2=beta, 3=alpha


def pinned() -> tuple[int, int]:
    src = BUILDER.read_text(encoding="utf-8")
    project = int(re.search(r"^ATM10_PROJECT\s*=\s*(\d+)", src, re.M).group(1))
    file_id = int(re.search(r"^ATM10_FILE\s*=\s*(\d+)", src, re.M).group(1))
    return project, file_id


def cf(path: str, key: str) -> dict:
    req = urllib.request.Request(
        f"{API}/{path}",
        headers={"x-api-key": key, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.load(r)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="move the pin and rebuild the egg")
    ap.add_argument("--github-output", action="store_true", help="emit GITHUB_OUTPUT lines")
    args = ap.parse_args()

    key = os.environ.get("CURSEFORGE_API_KEY")
    if not key:
        print("CURSEFORGE_API_KEY is not set", file=sys.stderr)
        return 2

    project, current = pinned()

    try:
        mod = cf(f"mods/{project}", key)["data"]
        files = cf(f"mods/{project}/files?pageSize=50", key)["data"]
    except urllib.error.HTTPError as exc:
        print(f"CurseForge API error: HTTP {exc.code}", file=sys.stderr)
        return 2

    # Only stable releases. Betas and alphas are how you lose a world -- ATM11's
    # current build describes itself as "Super early alpha".
    releases = sorted(
        (f for f in files if f["releaseType"] == RELEASE and f.get("serverPackFileId")),
        key=lambda f: f["fileDate"],
    )
    if not releases:
        print("no stable release with a server pack found", file=sys.stderr)
        return 2

    latest = releases[-1]
    behind = latest["id"] != current

    cur_meta = next((f for f in files if f["id"] == current), None)
    cur_name = cur_meta["displayName"] if cur_meta else f"file {current}"

    print(f"pack     : {mod['name']}")
    print(f"pinned   : {cur_name}  (file {current})")
    print(f"latest   : {latest['displayName']}  (file {latest['id']}, {latest['fileDate'][:10]})")
    print(f"status   : {'UPDATE AVAILABLE' if behind else 'up to date'}")

    if args.github_output and (out := os.environ.get("GITHUB_OUTPUT")):
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"behind={'true' if behind else 'false'}\n")
            fh.write(f"current_id={current}\n")
            fh.write(f"latest_id={latest['id']}\n")
            fh.write(f"latest_name={latest['displayName']}\n")
            fh.write(f"latest_date={latest['fileDate'][:10]}\n")
            fh.write(f"server_pack_id={latest['serverPackFileId']}\n")
            fh.write(f"pack_name={mod['name']}\n")

    if behind and args.write:
        src = BUILDER.read_text(encoding="utf-8")
        src = re.sub(
            r"^ATM10_FILE = \d+.*$",
            f"ATM10_FILE = {latest['id']}  # \"{latest['displayName']}\"",
            src, count=1, flags=re.M,
        )
        BUILDER.write_text(src, encoding="utf-8", newline="\n")
        subprocess.run([sys.executable, str(BUILDER)], check=True, cwd=REPO)
        print(f"\npin moved to {latest['id']} and the egg rebuilt")

        # A tiny descriptor friends can read to confirm they are on the right
        # version. Metadata only -- redistributing the pack itself would be
        # someone else's copyrighted work, and at 1.1 GB it would not fit in a
        # Discord attachment anyway.
        (REPO / "eggs" / "pinned-pack.json").write_text(
            json.dumps({
                "pack": mod["name"],
                "project_id": project,
                "file_id": latest["id"],
                "version": latest["displayName"],
                "released": latest["fileDate"][:10],
                "server_pack_file_id": latest["serverPackFileId"],
                "game_versions": latest["gameVersions"],
                "curseforge_url": f"https://www.curseforge.com/minecraft/modpacks/{mod['slug']}/files/{latest['id']}",
            }, indent=2) + "\n",
            encoding="utf-8", newline="\n",
        )
        print("wrote eggs/pinned-pack.json")

    return 10 if behind else 0


if __name__ == "__main__":
    raise SystemExit(main())
