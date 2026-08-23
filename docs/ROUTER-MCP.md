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

---

## Incident: DHCP reservation broke the wired connection

**Do not add a DHCP reservation for a MAC that already holds a live dynamic lease
for that same address on this router.**

What happened: a reservation was added for `A0-36-BC-BA-5A-C3 -> 192.168.0.115`
while that MAC already had an active lease for `.115`. Nothing broke immediately.
Just over an hour later the **120-minute lease came up for renewal**, and the router
stopped answering DHCP for that client entirely. Windows fell back to APIPA
(`169.254.x`) and silently failed over to WiFi.

Diagnosis that actually identified it:

```powershell
Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-Dhcp-Client/Admin'} -MaxEvents 5
```

Error `0x79` = `ERROR_SEM_TIMEOUT`: the DHCP server sent **no reply**. The adapter
itself was fine throughout -- `Up`, `Connected`, 2.5 Gbps, DHCP enabled. The router's
table showed the reservation *and* a stale dynamic lease for the same MAC/IP
simultaneously.

**Resolution: a static IP on Windows**, which is the better arrangement for a server
host anyway. The reservation is deliberately left in place so the router will never
hand `.115` to another device.

```powershell
Set-NetIPInterface -InterfaceAlias "Ethernet 5" -Dhcp Disabled
Remove-NetIPAddress -InterfaceAlias "Ethernet 5" -Confirm:$false
New-NetIPAddress -InterfaceAlias "Ethernet 5" -IPAddress 192.168.0.115 `
    -PrefixLength 24 -DefaultGateway 192.168.0.1
Set-DnsClientServerAddress -InterfaceAlias "Ethernet 5" -ServerAddresses 192.168.0.1
```

### Reservations cannot be deleted through this API

`admin/dhcps?form=reservation` supports `operation=load` and `operation=insert`
(that is what `add_ipv4_reservation` uses). `operation=remove` **is** a real callback
-- it dispatches -- but every key shape tried returned the router's Lua
`assertion failed`: bare MAC, entry JSON, row index, `index=`, IP, `[0]`, `[0]` as
JSON. `operation=delete` does not exist (`no such callback`).

Delete reservations in the web UI: **Advanced -> Network -> DHCP Server ->
Address Reservation**.

### Useful facts about this router

```
DHCP pool   192.168.0.2 - 192.168.0.253      (no room outside it for statics
lease time  120                               without shrinking the pool first)
LAN         192.168.0.1 / 255.255.255.0
WAN         <your-wan-ip>
```

`netinfo.py` dumps all of the above, including the raw DHCP settings the typed
dataclasses do not expose.
