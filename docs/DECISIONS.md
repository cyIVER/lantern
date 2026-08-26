# LANtern — Decision Record

Captured from the design interview. Every row is a resolved branch.

Entries are kept as they were written. Where a later decision overturned an
earlier one, the earlier one is marked **superseded** and left in place — the
reasoning that led to it is the part worth keeping, and deleting it would make
the same ground look unexplored next time. The most recent entry is
[2026-08-26: a bridged VM replaces Docker-inside-WSL2](#2026-08-26--a-bridged-vm-replaces-docker-inside-wsl2).

## Platform

> **Partly superseded 2026-08-26.** The runtime, storage and networking rows below
> describe Docker Desktop on WSL2. LANtern now runs on Docker CE inside a bridged
> VirtualBox VM. The *game* decisions — CS2, app ID 730, containerised Wings — are
> unchanged. See the 2026-08-26 entry.

| Decision | Choice | Why |
|---|---|---|
| Game | **CS2** (Steam app **730**) | Installed game is Counter-Strike 2, not CS:GO. CS:GO's dedicated server is no longer distributed by Valve. |
| App ID | **730**, not 740 | CS2 merged client + dedicated server into one app ID. 740 was CS:GO-only. |
| Runtime | **Docker** (Docker Desktop, WSL2 backend) | Keeps the server fully isolated from the Steam client on the same PC, and allows anonymous steamcmd downloads. |
| Storage | **Move Docker disk image to E:** | `docker_data.vhdx` is already 65 GB and C: has only 15.8 GB free. E: has 752 GB. Also gives native ext4 speed vs. a slow virtiofs bind mount. |
| Panel | **Pelican Panel + Wings, both containerised** | Wings does not run on Windows, but runs fine as a container driving Docker Desktop via the socket. Avoids the drvfs `chown` problem that breaks Wings on `/mnt/e`. |

### Evidence gathered

> **Superseded 2026-08-26.** Everything in this list was measured against the WSL2
> arrangement. It is accurate about what was true then and describes nothing that
> is true now.

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
| Connect | `connect 192.168.0.115:27015` — ~~WSL2 NAT blocks LAN broadcast discovery, so no server-browser entry~~ (superseded 2026-08-26: the VM is bridged; whether the LAN tab now populates is untested) |

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

> **Superseded 2026-08-26.** Risk 1 is resolved by removing Docker Desktop from the
> picture entirely; Wings now runs on ordinary Docker CE on Linux, which is the
> supported configuration. Risk 3 is long closed. Risk 2 still stands in a changed
> shape — see the 2026-08-26 entry.

1. **Pelican Wings on Docker Desktop is an unsupported configuration.** The blocking
   issues are solved, but this needs end-to-end verification before a LAN party.
2. **DHCP.** If the router reassigns this PC's IP, the panel URL and connect string
   change. A DHCP reservation for `192.168.0.115` is recommended.
3. **Disk.** The move to E: must complete before pulling ~35 GB of CS2 files.

---

# Verification: Wings on Docker Desktop

> **Superseded 2026-08-26.** This whole investigation exists because the daemon was
> Docker Desktop's. It is not any more, and on a plain Linux host none of it
> applies: Wings and the daemon share one filesystem and one namespace, so the
> property tested below holds trivially. Kept because the reasoning — *what exactly
> does Wings require of a Docker daemon* — is what to re-run against any future
> host, and because the storage constraint it derives still shapes the layout.

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

> **Superseded 2026-08-26.** The Wings volumes now live on the VM's own ext4 root,
> which is a virtual disk file on E:. The *rule* survives the move: Wings `chown`s
> every volume directory, so its volumes must sit on a filesystem that can honour
> that — never a passthrough of a Windows path.

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

- ~~**Run `docker compose` from inside Ubuntu WSL**, not Git Bash.~~ *(Superseded
  2026-08-26 — you run it on the VM over ssh, and no Windows shell is in the path
  to mangle anything.)* MSYS mangles Linux
  paths (`/var/run/docker.sock` became `C:\Program Files\Git\var` during testing).
  Sometimes it does so without erroring: a `ui` container recreated from Git Bash
  came up healthy with an *empty* `/volumes`, silently emptying the skin
  catalogue. Verify with `docker exec stack-ui-1 ls /volumes`.
- Bind-mount targets are auto-created by the daemon, so no `sudo mkdir` is needed
  for `/var/lib/pelican` and friends. *(Still true.)*
- **WSL mirrored networking blocks the LAN, and hides it from you.** *(Superseded
  2026-08-26: there is no WSL in the serving path. Read this as evidence for why
  the move happened, not as instructions.)* With
  `networkingMode=mirrored` in `~/.wslconfig`, inbound traffic to WSL is policed
  by the **Hyper-V** firewall -- a separate thing from the Windows firewall --
  which defaults to `DefaultInboundAction: Block` with `LoopbackEnabled: True`.
  Everything therefore works perfectly from the host and not at all from
  anywhere else, and you do not find out until a friend tries to connect.
  `stack/bootstrap/open-lan-firewall.ps1` adds one narrow allow rule per port.
  Diagnose with:

  ```powershell
  Get-NetFirewallHyperVVMSetting -PolicyStore ActiveStore
  Test-NetConnection 192.168.0.115 -Port 80   # from the host, and from a laptop
  ```

- **Two interfaces on one subnet makes every service intermittently
  unreachable.** *(Still current, with one change: this is now a Windows-host
  problem rather than a stack problem. The VM is bridged onto the host's physical
  adapter, so a wedged NIC still takes LANtern off the LAN even though nothing on
  Windows serves anything. The adapter in question is now the Realtek USB 2.5GbE
  one.)* The Intel I226-V in this box wedges: the link reports Up at
  2.5 Gbps, full duplex, zero errors, and passes no traffic. Windows marks the
  profile `NoTraffic` and falls back to Wi-Fi -- which sits on the *same*
  192.168.0.0/24. With two routes at equal metric, replies leave by the wrong
  interface and clients drop them, so the panel loads once and then does not.
  It presents as a web-app bug and is a NIC bug.

  Always identify the dead interface with a source-bound ping. Without `-S`
  the packet can leave via the healthy adapter and prove nothing:

  ```powershell
  ping -S 192.168.0.115 -n 3 192.168.0.1   # Ethernet
  ping -S 192.168.0.222 -n 3 192.168.0.1   # Wi-Fi
  ```

  `stack/bootstrap/fix-ethernet.ps1` pins the link to 1.0 Gbps -- the I226-V
  wedges specifically on 2.5 Gbps negotiation, and the router is gigabit
  anyway -- restarts the adapter, verifies, and only then offers to disable
  Wi-Fi.

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

---

# 2026-08-26 — a bridged VM replaces Docker-inside-WSL2

LANtern now runs on a **VirtualBox VM named `lantern`**: Ubuntu 26.04 LTS, Docker
CE from Docker's own apt repository, bridged onto the Windows host's Realtek USB
2.5GbE adapter. It holds `192.168.0.115` statically. Windows runs VirtualBox and
nothing else LANtern depends on.

## Why WSL2 was abandoned

Not a preference. WSL2 could not carry the one thing the project exists to do.

**CS2 gameplay is UDP 27015, and UDP could not be published to the LAN.** Three
separate mechanisms, each with its own failure:

- WSL2's default NAT publishes ports through a relay that binds IPv6 loopback
  only. Works from the host, invisible from anywhere else.
- `networkingMode=mirrored` swaps that for the Windows network stack, at which
  point inbound traffic is policed by the **Hyper-V** firewall — a separate thing
  from the Windows firewall — which defaults to `DefaultInboundAction: Block` with
  loopback still permitted. So everything works perfectly from the chair you are
  sitting in and not at all from anywhere else, and you find out when a friend
  tries to connect.
- Docker Desktop's port-publishing service on this machine refused to stay
  started.

Each has a workaround. `netsh` portproxy (`publish-to-lan.ps1`) forwards TCP and
**not UDP**. Hyper-V firewall rules (`open-lan-firewall.ps1`) open a hole in one
of three layers. None of them covers UDP end to end, which means none of them
covers CS2.

The rest was accumulated tax rather than a blocker: a `.cmd` shim to reach a bash
script, a logon-time scheduled task to sequence WSL then Docker Desktop then
compose, a pinned static IP on a Windows NIC, `/mnt/c` paths that only resolve in
one shell, and a MariaDB-to-`pelican_nw` attachment that lived in a Windows logon
script because there was nowhere better to put it.

## Why a bridged VM

From the router's point of view the VM is just another machine on the LAN. It has
its own MAC, its own address, and its own network stack. Every port is open, TCP
and UDP alike, and **nothing on Windows forwards, proxies or filters anything.**
There is no layer left to misconfigure.

It also collapses three environments into one. The daemon, the filesystem Wings
`chown`s, the shell you type into and the paths in the compose file are now all
the same Linux namespace — which is the configuration Pelican actually supports.

## Verified

- **UDP 27015 is reachable from the LAN**, from two separate machines, and CS2
  gameplay works over it. This had never once worked under WSL2.
- `192.168.0.115` survives a VM reboot. It is pinned in
  `/etc/netplan/50-cloud-init.yaml` with cloud-init's network management disabled,
  so cloud-init cannot regenerate the file and revert it.
- Docker 29.7.2 with Compose v5.5.0, from Docker's apt repo.

Not verified, and deliberately not claimed: whether the server now appears in
Steam's **LAN** server browser tab. The obstacle that made it impossible is gone.
Nobody has looked.

## What it cost

**Roughly 2x single-threaded CPU performance, and no AVX2 in the guest.**

VirtualBox cannot use hardware virtualisation directly here, because WSL2 keeps
the Windows Hypervisor Platform active and Windows will not hand VT-x to two
hypervisors. VirtualBox therefore runs *on top of* the Windows hypervisor, as a
nested guest. Single-threaded throughput lands around half of native, and AVX2 is
not exposed to the guest at all.

That is recoverable, at a price:

```powershell
bcdedit /set hypervisorlaunchtype off
```

which gives VirtualBox the hardware directly — and **disables WSL2 and Docker
Desktop** on that machine, since both depend on the same hypervisor. That trade
has not been taken: the machine is also a workstation, and a CS2 server for twelve
players is not CPU-bound in a way that 2x single-threaded matters. Revisit it if
Minecraft tick times or CS2 server frames say otherwise.

## Other consequences

| | |
|---|---|
| Repo | `/opt/lantern` on the VM — `/opt/lantern/stack`, `/opt/lantern/stardew` |
| Access | `ssh lantern` from Windows (alias in `C:\Users\iveri\.ssh\config`, key `lantern_vm`, VM user `iverson`) |
| Control script | symlinked to `/usr/local/bin/lantern`, so `lantern status` works anywhere on the VM |
| VM lifecycle | `VBoxManage startvm lantern --type headless` / `VBoxManage controlvm lantern acpipowerbutton`; serial console logged to `E:\LANtern-VM\serial.log` |
| Windows host | back on DHCP (currently `192.168.0.231`) and no longer part of serving anything |
| Resources | the VM holds 18 GB (17 usable) and 12 vCPUs of the host's 32 GB and 20 logical cores, for as long as it is running |

**The one-server-at-a-time rule stands, for the same reason as before.** The
budget barely moved: the VM is assigned 18 GB with about 17 GB usable, where
WSL2's allocation was also 18 GB. Minecraft alone claims 11.5 GB of it alongside
the panel stack, so there is still no arrangement in which two game servers and a
game client coexist. `lantern use <game>` enforces it rather than leaving it as a
habit.

**The MariaDB-to-`pelican_nw` attachment became a systemd timer.**
`lantern-dbnet.timer` runs every 60 seconds, idempotently, and re-attaches the
database container to the bridge Wings creates for game servers. It cannot be a
compose dependency, because Wings creates that network itself and only once it
starts — an `external: true` reference fails on a machine where no game server has
ever run. A timer rather than a one-shot because the network can appear at three
different moments: at boot, when Wings first starts, and when the first server is
created. This previously lived in a Windows logon script, which is exactly why it
did not survive the move on its own.

**A wedged NIC is still fatal, and is still a Windows problem.** Bridged mode
rides the host's physical adapter. `stack/bootstrap/fix-ethernet.ps1` remains
live for that reason and only that reason.

**The `.115` DHCP reservation is bound to the old MAC.** The reservation on the
router names the Windows NIC's MAC (`A0-36-BC-BA-5A-C3`); the VM's bridged adapter
has a different one. The VM holds the address statically so it does not need a
lease — but the router's pool is `192.168.0.2-253` with no exclusions, so it can
still hand `.115` to some other device and cause a conflict. Whether the
reservation has been re-pointed at the VM's MAC is not recorded here; check it.

## Scripts this retired

`stack/lantern.cmd`, and in `stack/bootstrap/`: `publish-to-lan.ps1`,
`open-lan-firewall.ps1`, `register-startup-task.ps1`, `lantern-startup.ps1`,
`set-service-ip.ps1`. They are listed as superseded in
[stack/bootstrap/README.md](../stack/bootstrap/README.md) rather than deleted, so
that anyone who finds one in the tree learns why not to run it.

The build, migration and cutover for all of this live in `vm/`.
