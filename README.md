# LANtern

A LAN game-server host: **CS2** (and Minecraft when you want it) running on Docker
Desktop, managed through Pelican Panel, with a purpose-built CS2 control UI and an
MCP surface so AI agents can help administer the network.

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
| **[CONTROL-UI.md](docs/CONTROL-UI.md)** | How the CS2 UI works, its API, and how to extend it. |
| **[DECISIONS.md](docs/DECISIONS.md)** | Why it is built this way, and the Wings-on-Docker-Desktop proof. |
| **[ROUTER-MCP.md](docs/ROUTER-MCP.md)** | Router tools for AI agents, plus the DHCP incident writeup. |
| **[SECRETS.md](docs/SECRETS.md)** | Every key and secret: where it lives, how to create it, how to rotate it. |
| **[stack/README.md](stack/README.md)** | Bringing the stack up, installer gotchas, recovery. |
| **[stack/bootstrap/README.md](stack/bootstrap/README.md)** | The automation scripts and the traps they encode. |

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

A **"LANtern startup"** scheduled task brings the panel stack up at logon in the
right order (Ubuntu WSL → Docker Desktop → compose). Game servers stay off until
you start them.

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
docs/           everything above
```

## The machine

| | |
|---|---|
| Host | `IVERSON_PC` — i5-13600KF, 32 GB, 2.5 GbE |
| Host IP | `192.168.0.115` — **static on Windows** ([why](docs/ROUTER-MCP.md)) |
| Router | TP-Link Archer BE230, firmware 1.2.5, at `192.168.0.1` |
| Storage | Docker **and** Ubuntu WSL both on **E:** — nothing game-related on C: |
| CS2 server | 66 GB on disk · 8 GB RAM / 120 GB cap · 12 slots · 7 plugins |

---

## Rebuilding from scratch

Run from inside **Ubuntu WSL**, not Git Bash — MSYS rewrites Linux paths and will
mangle the Docker socket mount.

```bash
# 1. panel stack
cd /mnt/c/Users/iveri/Documents/code/lantern/stack
docker run --rm -v stack_pelican-data:/d alpine mkdir -p /d/plugins
docker compose up -d

# 2. node, wings config, port allocations
docker compose exec -T panel php artisan tinker < bootstrap/create-node.php
bash bootstrap/install-wings-config.sh && docker compose restart wings
docker compose exec -T panel php artisan tinker < bootstrap/allocations.php

# 3. CS2 egg and server  (~66 GB download)
python ../eggs/build-egg.py
docker compose exec -T panel php artisan tinker < bootstrap/create-cs2-server.php

# 4. control UI
#    Map icons are NOT committed -- they are extracted from your own CS2 install.
cd ../ui && uv run extract-map-icons.py && cd ../stack
docker compose exec -T panel php artisan tinker < bootstrap/create-ui-credentials.php
docker compose up -d --build ui
```

## Health check

```bash
wsl -d Ubuntu-26.04 -e bash -lc "cd /mnt/c/Users/iveri/Documents/code/lantern/stack && bash bootstrap/cs2-status.sh"
```

## Still to do

- Minecraft (Paper) server — egg is imported, nothing created yet
- Friends registered as admins — send SteamID64s, see [USING.md](docs/USING.md)
