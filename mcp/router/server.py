"""LANtern router MCP server.

A read-only control surface over a TP-Link router (Archer BE230 / BE3600),
exposed as typed MCP tools so Claude Code and Codex can answer questions about
the LAN without screen-scraping a web UI.

Write operations are deliberately absent. See docs/ROUTER-MCP.md for the
approval-gated write design planned for the LANtern panel.
"""
from __future__ import annotations

import os

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from tplinkrouterc6u import Connection

import credentials
import router as rt

app = MCPServer(
    "lantern-router",
    version="0.1.0",
    instructions=(
        "Read-only access to the LAN's TP-Link router. Use these tools to answer "
        "questions about connected devices, DHCP leases and reservations, WiFi "
        "state and network configuration. No tool here can change router "
        "settings; call write_access_status() if asked to modify anything."
    ),
)

# Every tool is a pure read. Marking them explicitly lets MCP clients skip
# approval prompts and makes the read-only contract machine-checkable.
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)

# Bands worth probing on a dual-band Archer BE230 / BE3600.
BANDS = (
    Connection.HOST_2G,
    Connection.HOST_5G,
    Connection.GUEST_2G,
    Connection.GUEST_5G,
)


@app.tool(annotations=READ_ONLY)
def router_info() -> dict:
    """Model, hardware revision and firmware version of the router."""
    with rt.session() as client:
        return rt.dump(client.get_firmware())


@app.tool(annotations=READ_ONLY)
def network_status() -> dict:
    """Overall router health: WAN/LAN addressing, uptime, CPU and memory load,
    per-band radio state, and how many clients are connected."""
    with rt.session() as client:
        status = client.get_status()
    data = rt.dump(
        status,
        props=("wan_macaddr", "lan_macaddr", "wan_ipv4_addr", "lan_ipv4_addr",
               "wan_ipv4_gateway"),
    )
    # 'devices' is the full client list; list_devices() covers that in detail.
    data["devices"] = len(getattr(status, "devices", []) or [])
    return data


@app.tool(annotations=READ_ONLY)
def list_devices() -> list[dict]:
    """Every client currently connected, with MAC, IP, hostname, connection
    type (wired / 2.4G / 5G / guest), signal strength and throughput."""
    with rt.session() as client:
        devices = client.get_status().devices or []
    return [rt.dump(d, props=("macaddr", "ipaddr")) for d in devices]


@app.tool(annotations=READ_ONLY)
def dhcp_leases() -> list[dict]:
    """Current DHCP leases: MAC, assigned IP, hostname and remaining lease time."""
    with rt.session() as client:
        if not hasattr(client, "get_ipv4_dhcp_leases"):
            raise rt.RouterUnavailable("This router model does not expose DHCP leases.")
        leases = client.get_ipv4_dhcp_leases()
    return [rt.dump(l, props=("macaddr", "ipaddr")) for l in leases]


@app.tool(annotations=READ_ONLY)
def dhcp_reservations() -> list[dict]:
    """Static DHCP reservations (address bindings) configured on the router.

    Use this to confirm a host is pinned to a fixed IP -- for example that the
    LANtern game host still holds 192.168.0.115.
    """
    with rt.session() as client:
        if not hasattr(client, "get_ipv4_reservations"):
            raise rt.RouterUnavailable("This router model does not expose reservations.")
        reservations = client.get_ipv4_reservations()
    return [rt.dump(r, props=("macaddr", "ipaddr")) for r in reservations]


@app.tool(annotations=READ_ONLY)
def ipv4_status() -> dict:
    """Detailed IPv4 configuration: WAN connection type, netmask, DNS servers,
    LAN subnet and whether the DHCP server is enabled."""
    with rt.session() as client:
        status = client.get_ipv4_status()
    return rt.dump(
        status,
        props=("wan_macaddr", "wan_ipv4_ipaddr", "wan_ipv4_gateway",
               "wan_ipv4_netmask", "wan_ipv4_pridns", "wan_ipv4_snddns",
               "lan_macaddr", "lan_ipv4_ipaddr", "lan_ipv4_netmask"),
    )


@app.tool(annotations=READ_ONLY)
def wifi_status() -> dict:
    """Per-band WiFi configuration: SSID, whether the radio is on, encryption
    mode, channel and hidden-SSID state.

    WiFi passwords (psk_key, portal_password) are redacted and never returned.
    """
    out: dict[str, dict] = {}
    with rt.session() as client:
        if not hasattr(client, "get_wifi"):
            raise rt.RouterUnavailable("This router model does not expose WiFi details.")
        for band in BANDS:
            try:
                out[band.name] = rt.dump(client.get_wifi(band))
            except Exception as exc:  # Band absent on this hardware.
                out[band.name] = {"unavailable": str(exc)}
    return out


@app.tool(annotations=READ_ONLY)
def vpn_status() -> dict:
    """Whether OpenVPN / PPTP / IPSec servers are enabled and their client counts."""
    with rt.session() as client:
        if not hasattr(client, "get_vpn_status"):
            raise rt.RouterUnavailable("This router model does not expose VPN status.")
        return rt.dump(client.get_vpn_status())


@app.tool(annotations=READ_ONLY)
def write_access_status() -> dict:
    """Explain why no write tools exist and how changes are meant to be made.

    Call this when asked to change router settings, so the answer is accurate
    rather than an attempt at a tool that is not here.
    """
    return {
        "writes_available": False,
        "reason": (
            "This server is read-only by design. Router changes require human "
            "approval and re-entry of the admin password, which an agent cannot "
            "supply."
        ),
        "how_to_change_settings": [
            "Web UI: http://192.168.0.1 (Advanced > Network > DHCP Server for reservations)",
            "Planned: approval-gated writes via the LANtern panel -- an agent proposes "
            "a change, you approve it in the UI with your password, the panel applies it.",
        ],
        "credential_storage": "Windows Credential Manager (DPAPI), service 'lantern-router'",
        "password_configured": credentials.is_configured(),
        "router_host": rt.HOST,
    }


if __name__ == "__main__":
    app.run(transport="stdio")
