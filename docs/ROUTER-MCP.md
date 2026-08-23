# Router MCP Server

A read-only MCP control surface over the LAN's TP-Link router (Archer BE230 /
BE3600), so Claude Code and Codex can answer questions about the network with
typed tools instead of scraping a web UI.

## Why not SSH

The original plan was LAN-only SSH. That is not possible on this hardware:

- Port 22 on `192.168.0.1` returns **`Connection refused`** — no SSH daemon exists
- TP-Link's consumer Archer firmware ships no user-accessible SSH or telnet server
- The BE230/BE3600 is recent MediaTek Filogic Wi-Fi 7 hardware with **no OpenWrt support**

The router's only management surface is HTTP/80 and HTTPS/443. This server speaks
the same local API the TP-Link Tether app uses — no cloud, no TP-Link ID.

## Setup

Store the router password once. You type it into a hidden prompt, so it never
appears in a chat, a shell argument, a log, or agent context:

```bash
cd mcp/router && uv run set_password.py
```

Verify:

```bash
cd mcp/router && uv run verify.py
```

Claude Code picks up `.mcp.json` at the repo root automatically. For **Codex**,
add to `~/.codex/config.toml`:

```toml
[mcp_servers.lantern-router]
command = "uv"
args = ["--directory", "C:\Users\iveri\Documents\code\lantern\mcp\router", "run", "server.py"]
```

## Tools

All nine are marked `read_only_hint=True`.

| Tool | Returns |
|---|---|
| `router_info` | Model, hardware revision, firmware version |
| `network_status` | WAN/LAN addressing, uptime, CPU/memory, radio state, client counts |
| `list_devices` | Every connected client: MAC, IP, hostname, band, signal, throughput |
| `dhcp_leases` | Active leases with remaining time |
| `dhcp_reservations` | Static address bindings — e.g. confirming the game host holds `.115` |
| `ipv4_status` | WAN conn type, netmask, DNS, LAN subnet, DHCP server state |
| `wifi_status` | Per-band SSID, radio state, encryption, channel (**passwords redacted**) |
| `vpn_status` | OpenVPN / PPTP / IPSec state and client counts |
| `write_access_status` | Explains why writes are absent and how to make changes |

## Security model

Be precise about what this does and does not buy.

**What it guarantees**

- The router password is stored in **Windows Credential Manager**, DPAPI-encrypted
  at rest — not in `.env`, not in the repo, not in a dotfile
- No tool returns, logs, or embeds the password, including in error messages
- `get_wifi()` returns `psk_key` and `portal_password` **in plaintext**; both are
  stripped by `router.scrub()` before anything reaches an agent. Covered by a test
- No write tool exists, so no agent can change router state through this server

**What it does NOT guarantee**

This is a guard against **mistakes**, not against a malicious agent. Any process
running as your user can read your own Credential Manager, and an agent with shell
access could bypass this server and drive `tplinkrouterc6u` directly. Treat it as
accident-prevention and blast-radius control, not a security boundary.

## Operational constraint

> The TP-Link web interface permits exactly **one** logged-in session at a time.

So the server cannot hold a persistent session — it would lock you out of your own
router. Every tool call authorizes, acts, and logs out. Consequences:

- Calls take a second or two longer than a pooled connection would
- **Agent calls fail while you have the router web UI open in a browser.** The error
  says so explicitly rather than surfacing a raw traceback
- A module-level lock serializes concurrent tool calls

## Planned: approval-gated writes

Writes are intentionally not in v1. The intended design, once the LANtern panel exists:

1. An agent calls a write tool, which returns a **proposal** — never an applied change
2. The proposal appears in the LANtern panel as a pending action with a diff
3. You approve it in the UI and **re-enter the router password**
4. The panel applies the change; the agent is told the outcome

The agent proposes, a human with the password disposes. Candidate writes:
`add_ipv4_reservation`, `set_wifi(band, enable)`, `set_ipv4_dhcps`, `reboot`.
