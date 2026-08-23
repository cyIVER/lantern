# LANtern

A LAN game-server host and control panel: CS2 and Minecraft on Docker Desktop,
managed through Pelican Panel, with an MCP control surface so AI agents can help
administer the network.

Named for a beacon on the LAN.

## Status

| Component | State |
|---|---|
| Design decisions | ✅ Recorded in [docs/DECISIONS.md](docs/DECISIONS.md) |
| Docker storage on E: | ✅ Migrated (65 GB reclaimed from C:) |
| Router MCP server | ✅ Built, read-only, 9 tools — [docs/ROUTER-MCP.md](docs/ROUTER-MCP.md) |
| Pelican Panel + Wings stack | ⬜ Next |
| CS2 egg with `MODE` variable | ⬜ Pending |
| Plugin bootstrap | ⬜ Pending |
| Minecraft (Paper) | ⬜ Pending |

## Layout

```
stack/       docker compose for Pelican Panel + Wings
eggs/        custom Pelican egg definitions (CS2 with mode switching)
cs2/         server configs and plugin manifests
minecraft/   Paper server config
mcp/router/  read-only MCP control surface for the TP-Link router
docs/        decision record and component docs
```

## The LAN

| | |
|---|---|
| Host | `IVERSON_PC` — i5-13600KF, 32 GB, 2.5 GbE |
| Host IP | `192.168.0.115` (DHCP reservation on the router) |
| Router | TP-Link Archer BE230 / BE3600 at `192.168.0.1` |
| Panel | `http://192.168.0.115` (planned) |
| CS2 | `connect 192.168.0.115:27015` |

## Quick start

```bash
cd mcp/router && uv run set_password.py && uv run verify.py
```
