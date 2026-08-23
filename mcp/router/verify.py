"""Smoke-test the router connection outside of MCP.

    uv run verify.py

Prints nothing sensitive: no password, no WiFi PSK.
"""
from __future__ import annotations

import credentials
import router as rt

def main() -> int:
    print(f"Router host   : {rt.HOST}")
    print(f"Password set  : {credentials.is_configured()}")
    if not credentials.is_configured():
        print("\nRun 'uv run set_password.py' first.")
        return 1
    try:
        with rt.session() as client:
            fw = client.get_firmware()
            status = client.get_status()
            print(f"Model         : {fw.model}")
            print(f"Firmware      : {fw.firmware_version}")
            print(f"Clients       : {status.clients_total} "
                  f"(wired {status.wired_total}, wifi {status.wifi_clients_total})")
            print(f"LAN IP        : {status.lan_ipv4_addr}")
            print("\nConnection OK.")
    except rt.RouterUnavailable as exc:
        print(f"\nFAILED:\n{exc}")
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
