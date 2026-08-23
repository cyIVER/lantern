# LANtern — Decision Record

Captured from the design interview. Every row is a resolved branch.

## Platform

| Decision | Choice | Why |
|---|---|---|
| Game | **CS2** (Steam app **730**) | Installed game is Counter-Strike 2, not CS:GO. CS:GO's dedicated server is no longer distributed by Valve. |
| App ID | **730**, not 740 | CS2 merged client + dedicated server into one app ID. 740 was CS:GO-only. |
| Runtime | **Docker** (Docker Desktop, WSL2 backend) | Keeps the server fully isolated from the Steam client on the same PC, and allows anonymous steamcmd downloads. |
| Storage | **Move Docker disk image to E:** | `docker_data.vhdx` is already 65 GB and C: has only 15.8 GB free. E: has 752 GB. Also gives native ext4 speed vs. a slow virtiofs bind mount. |
| Panel | **Pelican Panel + Wings, both containerised** | Wings does not run on Windows, but runs fine as a container driving Docker Desktop via the socket. Avoids the drvfs `chown` problem that breaks Wings on `/mnt/e`. |

### Evidence gathered

- Docker Desktop's daemon **can** bind-mount Ubuntu WSL paths (verified empirically).
- Ubuntu 26.04 WSL already runs **systemd**, cgroups are **v2**.
- WSL is in **NAT** mode; Ubuntu is at `172.26.12.36` and is not LAN-reachable.
  Not a problem: Docker Desktop publishes container ports on the Windows host.
- Ports 80, 443, 8080, 8443, 2022, 3306, 25565, 27015, 27020 are all **free**.
- Host: i5-13600KF, 14C/20T, 32 GB RAM. Ample to game and host at once.

## CS2 server

| Decision | Choice |
|---|---|
| Audience | **Same LAN only** → clean `sv_lan 1`, no GSLT needed |
| Slots | **12**, `bot_quota_mode fill` so teams stay even |
| Mode switching | **One server, `MODE` startup variable** in the egg (competitive / retakes / practice / dm), selectable in the panel UI |
| Maps | Active duty + fan favourites (all official, no download friction) |
| CSTV | **On**, UDP 27020 — spectators don't consume player slots |
| Demos | **On**, auto-recorded per match by MatchZy |
| Updates | **Validate/update on every start** — a version mismatch hard-blocks clients |
| Connect | `connect 192.168.0.115:27015` — WSL2 NAT blocks LAN broadcast discovery, so no server-browser entry |

### Plugin stack

Base: **Metamod:Source** + **CounterStrikeSharp**

| Plugin | Purpose | Active in mode |
|---|---|---|
| MatchZy | Knife round, `.ready`, MR12, pauses, backups, demos | competitive |
| CS2-SimpleAdmin | kick/ban/mute/slay/swap/map | all |
| WeaponPaints | Knives, gloves, skins for everyone (needs MySQL) | all |
| cs2-practicemode | Nade lineups, `.rethrow`, bot placement | practice |
| CS2-Retakes | Retake rounds | retakes |

> **Constraint:** MatchZy, Retakes and PracticeMode each take over round flow and
> conflict if loaded together. The `MODE` variable enables exactly one set per boot.

## Other

| Decision | Choice |
|---|---|
| Minecraft | **Paper**, latest, via Pelican's egg |
| Panel URL | `http://192.168.0.115` (port 80, plain HTTP, LAN-only) |
| Panel auth | Admin (you) + **limited subusers** for trusted friends |
| Autostart | **Panel stack only** (`restart: unless-stopped`); game servers started on demand from the UI |
| Secrets | Generated into a git-ignored `.env` |

## Known risks

1. **Pelican Wings on Docker Desktop is an unsupported configuration.** The blocking
   issues are solved, but this needs end-to-end verification before a LAN party.
2. **DHCP.** If the router reassigns this PC's IP, the panel URL and connect string
   change. A DHCP reservation for `192.168.0.115` is recommended.
3. **Disk.** The move to E: must complete before pulling ~35 GB of CS2 files.

---

# Verification: Wings on Docker Desktop

Pelican's docs say Wings does not run on Windows. That is true of the *binary*.
Running Wings **as a container** against Docker Desktop is a different question,
and it hinges on one property.

## The property that had to hold

Wings does not copy files into game containers. It tells the daemon *"bind-mount
`/var/lib/pelican/volumes/<uuid>` into this container."* That only works if Wings
and the daemon resolve that path to the **same real directory**.

On native Linux this is trivial. On Docker Desktop the "host" is the `docker-desktop`
LinuxKit VM, not Ubuntu — and `/var/lib/` exists in **both**, so a silent collision
was plausible.

## Tests run

A bind test using `/home/iiverson/...` proves nothing: that path exists only in Ubuntu.
And testing via the `docker` CLI *inside Ubuntu* also proves nothing, because Docker
Desktop's WSL integration rewrites bind paths for the calling distro — Wings has no
such wrapper; it speaks the raw socket from inside a container.

The real test is **container → socket → daemon**, on a colliding path:

| Test | Result | Conclusion |
|---|---|---|
| Socket-originated bind of `/tmp/pelican-probe` | Returned Ubuntu's file content | Resolves to Ubuntu |
| Socket-originated bind of `/var/lib/docker` | 0 entries (VM's copy is populated) | Not the VM namespace |
| `/var/lib/docker` in Ubuntu afterwards | **Auto-created by the daemon** | Daemon writes into Ubuntu |
| Socket-originated bind of `/var/lib/containerd` | 0 entries | Not the VM namespace |
| Socket-originated bind of `/` | `snap`, `lost+found`, `home`, `init` | That is the Ubuntu root |

**Conclusion:** Docker Desktop's daemon treats the Ubuntu WSL filesystem as "the host"
for bind mounts, including for socket-originated requests from inside a container.
This is exactly what Wings requires. Re-verified after the storage move below.

## Consequence for storage

Wings volumes must therefore live in **Ubuntu's ext4 filesystem**. They cannot live on
`/mnt/e`, because drvfs cannot honour the `chown` Wings performs on every volume
directory. Ubuntu's vhdx was still on C:, so it was moved:

```
wsl --manage Ubuntu-26.04 --move E:\WSL\Ubuntu-26.04
```

Final layout — nothing game-related on C: any more:

| Store | Location | Size |
|---|---|---|
| Docker data | `E:\DockerData\DockerDesktopWSL` | 70 GB |
| Ubuntu WSL (holds `/var/lib/pelican`) | `E:\WSL\Ubuntu-26.04` | 13.8 GB, 945 GB free |

## Operational notes

- **Run `docker compose` from inside Ubuntu WSL**, not Git Bash. MSYS mangles Linux
  paths (`/var/run/docker.sock` became `C:\Program Files\Git\var` during testing).
- Bind-mount targets are auto-created by the daemon, so no `sudo mkdir` is needed
  for `/var/lib/pelican` and friends.

---

# Plugin archives can destroy the platform

The single hardest failure of the build, worth understanding before adding any
new plugin.

## Symptom

`!knife`, `!kick` and every other chat command silently do nothing. No error in
game, no error in the panel. The server runs, players connect, rounds play.

## Cause

`boot.sh` activates a mode by overlaying a staged plugin set's whole tree onto
`csgo/`. That is fine for a plugin that ships only its own files — but
**CS2-Practice-Plugin ships the entire platform**:

```
practice/addons/metamod/                    <- a 2024 Metamod
practice/addons/metamod.vdf
practice/addons/counterstrikesharp/bin/     <- a 2024 CounterStrikeSharp
practice/addons/counterstrikesharp/dotnet/
practice/addons/counterstrikesharp/gamedata/gamedata.json   <- engine signatures
practice/addons/counterstrikesharp/configs/core.example.json
```

Switching to practice overwrote all of it, in three escalating failures:

| What was overwritten | Result |
|---|---|
| `addons/metamod/` | `MMS: Fatal error: Detected engine 26 but could not load: metamod.2.cs2.so: undefined symbol: UtlMemory_CalcNewAllocationCount` |
| `counterstrikesharp/bin/` | `[META] Failed to load counterstrikesharp.so: undefined symbol: _ZN24CUtlMemoryBlockAllocator5PurgeEv` |
| `gamedata/gamedata.json` | `CSSharp: Failed to find signature for 'Host_Say'` … then **segfault** |

Metamod loads CounterStrikeSharp, which loads every plugin. Break the bottom of
that stack and everything above it vanishes without a word.

Deleting `addons/metamod` to reinstall has its own trap: **`counterstrikesharp.vdf`
lives in there** and is not in the Metamod tarball. Lose it and CSSharp never
loads even with a perfect Metamod.

## Fix

`normalize-plugins.sh` strips platform-owned paths at staging time, per file
rather than per directory, because `gamedata/` and `configs/` are legitimately
shared between platform and plugins:

| Path | Verdict |
|---|---|
| `addons/metamod/`, `addons/metamod*.vdf` | always stripped |
| `counterstrikesharp/{bin,dotnet,api,source}` | always stripped |
| `counterstrikesharp/gamedata/gamedata.json`, `schema_*.txt` | stripped (platform) |
| `counterstrikesharp/gamedata/<plugin>.json` | kept (WeaponPaints needs its own) |
| `counterstrikesharp/configs/plugins/` | kept |
| `counterstrikesharp/configs/*` (anything else) | stripped |
| `counterstrikesharp/{plugins,shared,lang}` | kept |

`repair-platform.sh` reinstalls Metamod and CSSharp when an install is already
damaged, preserving any `.vdf` across the swap.

## Practice mode is gone

Even with a clean platform, CSPracc v1.0.0.3 segfaults on load against current
CS2 — which is *why* it bundles a 2024 runtime: it requires that era. `practice`
was removed from the MODE enum in the egg and the UI, and `boot.sh` falls back to
competitive rather than boot-looping a crash.

## Lesson for the test suite

The original `test-modes.sh` asserted that plugin *directories* were staged. It
passed cheerfully through all of this, while nothing was loading at all. It now
asserts on CounterStrikeSharp's `Finished loading plugin` output. **Test the
observable behaviour, not the arrangement of files you just arranged.**
