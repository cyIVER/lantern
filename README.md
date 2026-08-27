# LANtern

A LAN game-server host: **CS2**, **Minecraft** and **Stardew Valley**, one at a
time, running on Docker and managed through Pelican Panel — with a landing page
that starts and stops them, purpose-built control UIs for the games, and an MCP
surface so AI agents can help administer the network.

The stack runs on a bridged Linux VM (`lantern`, Ubuntu 26.04) on the Windows box.
From the router's point of view it is simply another machine on the LAN.

Named for a beacon on the LAN.

---

## Open these

| What | Where | Use it for |
|---|---|---|
| 🏮 **LANtern** | **<http://192.168.0.115:8090>** | The landing page: start or stop a game, and the host dashboard. Start here. |
| 🎮 **CS2 Control** | **<http://192.168.0.115:8090/cs2>** | Players, maps, modes, match control, RCON console. No login. |
| 🌾 **Stardew Control** | **<http://192.168.0.115:8092>** | The farm: invite code, players, mods, farm state. No login. |
| 🛠 **Pelican Panel** | **<http://192.168.0.115>** | Files, backups, schedules, subusers, creating more servers. Login required. |
| 🔫 **Play** | `connect 192.168.0.115:27015` | Paste into the CS2 console |

The CS2 UI used to be at `:8090` itself. It moved to `/cs2` when the landing page
took the root; every other path under `:8090` is unchanged, so bookmarks to the
API still work.

**Friends joining?** → **[docs/CONNECTING.md](docs/CONNECTING.md)**

---

## Documentation

| Doc | What's in it |
|---|---|
| **[CONNECTING.md](docs/CONNECTING.md)** | How you and your friends join. Start here for a LAN party. |
| **[USING.md](docs/USING.md)** | Day-to-day: maps, modes, moderation, admin commands. |
| **[MINECRAFT.md](docs/MINECRAFT.md)** | All the Mods 10: how friends join, how it is run and updated. |
| **[STARDEW.md](docs/STARDEW.md)** | The always-on farm: its own control UI, mods, save import, Steam login. |
| **[CONTROL-UI.md](docs/CONTROL-UI.md)** | The landing page and the CS2 UI: how they work, their API, how to extend them. |
| **[DECISIONS.md](docs/DECISIONS.md)** | Why it is built this way, including the move to a bridged VM and the hypervisor trade. |
| **[ROUTER-MCP.md](docs/ROUTER-MCP.md)** | Router tools for AI agents, plus the DHCP incident writeup. |
| **[SECRETS.md](docs/SECRETS.md)** | Every key and secret: where it lives, how to create it, how to rotate it. |
| **[vm/README.md](vm/README.md)** | The VM itself: starting it, servicing it, and the backups. |
| **[stack/README.md](stack/README.md)** | Bringing the stack up, installer gotchas, recovery. |
| **[stack/bootstrap/README.md](stack/bootstrap/README.md)** | The automation scripts and the traps they encode. |

### Getting a shell on it

Everything below runs **on the VM**, over ssh from Windows:

```powershell
ssh lantern
```

That is an alias in `C:\Users\iveri\.ssh\config` for `iverson@192.168.0.115`,
authenticating with `C:\Users\iveri\.ssh\lantern_vm`. Any terminal will do —
PowerShell, Windows Terminal, Git Bash — because the shell you get is the VM's,
not Windows'.

The `lantern` control script is symlinked into `/usr/local/bin`, so it works from
any directory on the VM:

```bash
lantern status
lantern use cs2
lantern use minecraft
lantern stop
```

### Starting and stopping the VM

**Start it from the Desktop shortcut, "Start LANtern".** It runs
`vm\Start-LANtern.cmd`, which is `VBoxManage startvm lantern --type headless`.
Give it about 40 seconds, then open <http://192.168.0.115:8090>.

**Nothing starts the VM automatically.** The old "LANtern startup" scheduled task
was unregistered and Docker Desktop was taken out of the Run key when the stack
moved off WSL; nothing replaced them. After a Windows reboot LANtern is off until
you ask for it.

From any Windows shell, equivalently:

```powershell
VBoxManage startvm lantern --type headless
VBoxManage controlvm lantern acpipowerbutton     # graceful shutdown
```

`acpipowerbutton`, not `poweroff` — the latter is the equivalent of pulling the
plug on a machine holding a live MariaDB and a Minecraft world. If the VM never
comes up far enough to answer ssh, the serial console is logged to
`E:\LANtern-VM\serial.log`; that is where boot failures are readable.

One thing that surprises people: **Wings restores whichever game server was
running when the VM went down.** The VM does not autostart, but the game inside
it effectively does. Check the landing page rather than assuming it came up idle.

---

## What's running

Every port below is on the VM, reachable directly from the LAN.

| Service | Port | Autostarts | Notes |
|---|---|---|---|
| Pelican Panel | `80` | ✅ | Caddy + PHP-FPM + queue worker |
| LANtern landing + CS2 Control UI | `8090` | ✅ | One FastAPI service: `/` is the landing page, `/cs2` the CS2 UI. No auth, LAN only |
| Stardew Control UI | `8092` | with Stardew | Started with the farm and deliberately not stopped with it |
| Stardew HTTP API | `8091` | with Stardew | JunimoServer's own REST API |
| Stardew web VNC | `5800` | with Stardew | Password-protected; drives the running game |
| Minecraft Control UI | `8093` | — | Reserved. Being built separately; the landing page links to it only once something answers there |
| Wings daemon | `8080` | ✅ | Panel ↔ game server |
| SFTP | `2022` | ✅ | Panel file manager |
| MariaDB / Redis | internal | ✅ | No published ports |
| **CS2 server** | `27015` + `27020` | on demand | 27020 is CSTV — spectate without using a player slot |
| **Minecraft** | `25565`, RCON `25575` | on demand | All the Mods 10 |
| **Stardew** | `24642/udp`, query `27030` | on demand | Joined by Steam invite code, not by address |

The compose services are `restart: unless-stopped`, so the panel stack comes back
on its own whenever the VM boots. Game servers are started on demand — but see
the note above about Wings restoring the one that was running at shutdown.

**Only one game server runs at a time**, enforced by the control service behind
the landing page and by the `lantern` script on the VM. CS2 is allocated 8 GB and
Minecraft 11 GB on a box with about 17.6 GB usable; two at once is not slow, it is
the kernel OOM killer ending one of them mid-save.

**There is no firewall on the VM, deliberately.** `ufw` would not protect the
published ports anyway: Docker inserts its own nftables rules ahead of ufw's, so a
`ufw deny` on a published port is the illusion of protection rather than
protection. This is a home LAN behind NAT, and the panel has real authentication.
If a port is not answering, the service behind it is down.

A systemd timer, `lantern-dbnet.timer`, re-attaches MariaDB to Wings' `pelican_nw`
network every 60 seconds. It is idempotent and normally invisible; without it the
skin plugin inside a game container cannot resolve the `database` host and every
loadout silently reads as empty. See [vm/README.md](vm/README.md).

---

## Layout

```
stack/          docker compose: Pelican + Wings + MariaDB + Redis + UI
  bootstrap/    idempotent setup & repair scripts
ui/             landing page + CS2 control UI (FastAPI + vanilla JS, no build step)
  app/          backend: Pelican client API, direct RCON, server switching, host stats
  static/       shell.* is the landing page; index.* is the CS2 UI; maps/ holds
                icons extracted from the game
stardew-ui/     Stardew control UI, its own application on 8092
stardew/        the Stardew compose project (JunimoServer)
eggs/           the LANtern CS2 and Minecraft eggs
  src/          install.sh, boot.sh, normalize-plugins.sh (assembled by build-egg.py)
mcp/router/     read-only MCP tools for the TP-Link router
vm/             building, servicing and backing up the bridged VM
docs/           everything above
```

On the VM this tree lives at **`/opt/lantern`** — so `/opt/lantern/stack`,
`/opt/lantern/stardew`, and so on.

## The machine

| | |
|---|---|
| VM | `lantern` — VirtualBox, Ubuntu 26.04 LTS, 18 GB RAM (~17.6 usable), 12 vCPUs, 4 GB swap |
| VM IP | `192.168.0.115` — **static via netplan**, bridged onto the LAN |
| Windows host | i5-13600KF, 32 GB, 20 logical cores. On DHCP (currently `192.168.0.231`); runs VirtualBox and nothing else LANtern needs |
| Hypervisor | **off** (`hypervisorlaunchtype off`), so VirtualBox has native VT-x. WSL2 and Docker Desktop do not work on this machine as a result |
| NIC | Realtek USB 2.5GbE — the VM's bridge rides this adapter |
| Router | TP-Link Archer BE230, firmware 1.2.5, at `192.168.0.1` |
| Storage | VM disk on **E:** (Samsung SSD). Backups on **D:** (Toshiba HDD) — a different physical disk on purpose |
| CS2 server | 66 GB on disk · 8 GB RAM / 120 GB cap · 12 slots · 7 plugins |

The address is held by the VM, not by Windows. `/etc/netplan/50-cloud-init.yaml`
pins it and cloud-init's network management is disabled, so a reboot does not
revert it. The router's DHCP reservation for `.115` points at the VM's bridged
MAC, `08:00:27:F2:63:BA`.

E: has 669.6 GB free since the WSL and Docker Desktop virtual disks were removed
— 449 GB of it reclaimed in that one step.

## Backups

They exist, they run nightly, and they land on a different physical disk.

| | |
|---|---|
| Nightly data | Windows scheduled task **"LANtern backup"**, 03:00 → `D:\LANtern-Backups\data` |
| What is in it | ~165 MB: panel DB (incl. `cs2_weaponpaints`), `/etc/pelican` node token, Minecraft world, CS2 cfg + addons, Stardew saves + config, the gitignored `.env` files |
| Whole-VM image | `vm\export-vm-image.ps1` → `D:\LANtern-Backups\images`, by hand, VM powered off |

The task exits quietly when the VM is off, which is the normal case. CS2's ~67 GB
of game content is deliberately not backed up — SteamCMD fetches it again.

Full detail, including how to restore: [vm/README.md](vm/README.md).

---

## Rebuilding from scratch

Run on the VM (`ssh lantern`).

```bash
# 1. panel stack
cd /opt/lantern/stack
docker run --rm -v stack_pelican-data:/d alpine mkdir -p /d/plugins
docker compose up -d

# 2. node, wings config, port allocations
docker compose exec -T panel php artisan tinker < bootstrap/create-node.php
bash bootstrap/install-wings-config.sh && docker compose restart wings
docker compose exec -T panel php artisan tinker < bootstrap/allocations.php

# 3. CS2 egg and server  (~66 GB download)
python3 ../eggs/build-egg.py
docker compose exec -T panel php artisan tinker < bootstrap/create-cs2-server.php

# 4. control UI
#    Map icons are NOT committed -- they are extracted from a CS2 install. The
#    extractor has only ever been run against the Windows CS2 client, so run it
#    there and copy ui/static/maps/ across.
docker compose exec -T panel php artisan tinker < bootstrap/create-ui-credentials.php
docker compose up -d --build ui

# 5. host services (MariaDB <-> pelican_nw timer, swap, the lantern symlink)
bash ../vm/install-vm-services.sh
```

Then, once on Windows, in an elevated PowerShell — this writes the Desktop
shortcut and registers the nightly backup:

```powershell
vm\windows-setup.ps1
```

Rebuilding the **VM itself** rather than the stack inside it is
`vm/build-lantern-vm.sh` — though it was written to run from WSL, and there is no
WSL on this machine any more. See [vm/README.md](vm/README.md).

## Health check

```bash
ssh lantern 'cd /opt/lantern/stack && bash bootstrap/cs2-status.sh'
```

## Still to do

- The Minecraft control UI on `:8093` — being built separately. The landing page
  already links to it, gated on a server-side TCP probe, so the link appears the
  moment it is deployed and never before.
- **A real Minecraft LAN session.** The server installs, boots, answers RCON and
  accepts a TCP connection on 25565, and the world is in the nightly backup — but
  no actual player has ever connected to it. Everything about joining in
  [MINECRAFT.md](docs/MINECRAFT.md) is written from the pack's requirements, not
  from having watched someone do it.
- Whether the CS2 server appears in Steam's **LAN** server browser tab. The
  obstacle that made it impossible is gone; nobody has looked.
- Friends registered as admins — send SteamID64s, see [USING.md](docs/USING.md)
