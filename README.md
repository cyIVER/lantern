# LANtern

A LAN game-server host: **CS2** (and Minecraft when you want it) running on Docker,
managed through Pelican Panel, with a purpose-built CS2 control UI and an MCP
surface so AI agents can help administer the network.

The stack runs on a bridged Linux VM (`lantern`, Ubuntu 26.04) on the Windows box.
From the router's point of view it is simply another machine on the LAN.

Named for a beacon on the LAN.

---

## Open these

| What | Where | Use it for |
|---|---|---|
| 🎮 **CS2 Control** | **<http://192.168.0.115:8090>** | Players, maps, modes, match control, RCON console. No login. |
| 🛠 **Pelican Panel** | **<http://192.168.0.115>** | Files, backups, schedules, subusers, creating more servers. Login required. |
| 🔫 **Play** | `connect 192.168.0.115:27015` | Paste into the CS2 console |

**Friends joining?** → **[docs/CONNECTING.md](docs/CONNECTING.md)**

---

## Documentation

| Doc | What's in it |
|---|---|
| **[CONNECTING.md](docs/CONNECTING.md)** | How you and your friends join. Start here for a LAN party. |
| **[USING.md](docs/USING.md)** | Day-to-day: maps, modes, moderation, admin commands. |
| **[MINECRAFT.md](docs/MINECRAFT.md)** | All the Mods 10: how friends join, how it is run and updated. |
| **[STARDEW.md](docs/STARDEW.md)** | The always-on farm: its own control UI, mods, save import, Steam login. |
| **[CONTROL-UI.md](docs/CONTROL-UI.md)** | How the CS2 UI works, its API, and how to extend it. |
| **[DECISIONS.md](docs/DECISIONS.md)** | Why it is built this way, including the move to a bridged VM. |
| **[ROUTER-MCP.md](docs/ROUTER-MCP.md)** | Router tools for AI agents, plus the DHCP incident writeup. |
| **[SECRETS.md](docs/SECRETS.md)** | Every key and secret: where it lives, how to create it, how to rotate it. |
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

From Windows, in any shell:

```powershell
VBoxManage startvm lantern --type headless
VBoxManage controlvm lantern acpipowerbutton     # graceful shutdown
```

`acpipowerbutton`, not `poweroff` — the latter is the equivalent of pulling the
plug on a machine holding a live MariaDB and a Minecraft world. If the VM never
comes up far enough to answer ssh, the serial console is logged to
`E:\LANtern-VM\serial.log`; that is where boot failures are readable.

---

## What's running

| Service | Port | Autostarts | Notes |
|---|---|---|---|
| Pelican Panel | `80` | ✅ | Caddy + PHP-FPM + queue worker |
| CS2 Control UI | `8090` | ✅ | FastAPI, no auth, LAN only |
| Wings daemon | `8080` | ✅ | Panel ↔ game server |
| SFTP | `2022` | ✅ | Panel file manager |
| MariaDB / Redis | internal | ✅ | No published ports |
| **CS2 server** | `27015` | ❌ on demand | Start from either UI |
| CSTV | `27020` | with CS2 | Spectate without using a player slot |
| Minecraft | `25565` | — | Port reserved; Paper egg imported, no server yet |

The compose services are `restart: unless-stopped`, so the panel stack comes back
on its own whenever the VM boots. Game servers stay off until you start them.

Starting the **VM** after a Windows reboot is still a manual `VBoxManage startvm`
— no autostart is configured.

A systemd timer, `lantern-dbnet.timer`, re-attaches MariaDB to Wings' `pelican_nw`
network every 60 seconds. It is idempotent and normally invisible; without it the
skin plugin inside a game container cannot resolve the `database` host and every
loadout silently reads as empty. See `vm/install-vm-services.sh`.

---

## Layout

```
stack/          docker compose: Pelican + Wings + MariaDB + Redis + UI
  bootstrap/    idempotent setup & repair scripts
ui/             CS2 control UI (FastAPI + vanilla JS, no build step)
  app/          backend: Pelican client API + direct RCON
  static/       frontend; static/maps/ holds icons extracted from the game
eggs/           the LANtern CS2 egg
  src/          install.sh, boot.sh, normalize-plugins.sh (assembled by build-egg.py)
mcp/router/     read-only MCP tools for the TP-Link router
vm/             building, migrating to and servicing the bridged VM
docs/           everything above
```

On the VM this tree lives at **`/opt/lantern`** — so `/opt/lantern/stack`,
`/opt/lantern/stardew`, and so on.

## The machine

| | |
|---|---|
| VM | `lantern` — VirtualBox, Ubuntu 26.04 LTS, 18 GB RAM (17 usable), 12 vCPUs |
| VM IP | `192.168.0.115` — **static via netplan**, bridged onto the LAN |
| Windows host | i5-13600KF, 32 GB, 20 logical cores. On DHCP (currently `192.168.0.231`); runs VirtualBox and nothing else LANtern needs |
| NIC | Realtek USB 2.5GbE — the VM's bridge rides this adapter |
| Router | TP-Link Archer BE230, firmware 1.2.5, at `192.168.0.1` |
| Storage | VM disk on **E:** — nothing game-related on C: |
| CS2 server | 66 GB on disk · 8 GB RAM / 120 GB cap · 12 slots · 7 plugins |

The address is held by the VM, not by Windows. `/etc/netplan/50-cloud-init.yaml`
pins it and cloud-init's network management is disabled, so a reboot does not
revert it.

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

# 5. host services (MariaDB <-> pelican_nw timer, the lantern symlink)
bash ../vm/install-vm-services.sh
```

Rebuilding the **VM itself** rather than the stack inside it is `vm/build-lantern-vm.sh`.

## Health check

```bash
ssh lantern 'cd /opt/lantern/stack && bash bootstrap/cs2-status.sh'
```

## Still to do

- Minecraft (Paper) server — egg is imported, nothing created yet
- Friends registered as admins — send SteamID64s, see [USING.md](docs/USING.md)
