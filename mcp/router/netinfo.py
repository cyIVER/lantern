"""Dump the router's IPv4/LAN settings, including the DHCP pool if exposed.

    uv run netinfo.py
"""
from __future__ import annotations

import router as rt


def main() -> int:
    """Dump router IPv4 configuration and DHCP pool settings."""
    with rt.session() as client:
        st = client.get_ipv4_status()
        print("=== IPv4 status ===")
        for k, v in rt.dump(
            st,
            props=("wan_macaddr", "wan_ipv4_ipaddr", "wan_ipv4_gateway",
                   "wan_ipv4_netmask", "wan_ipv4_pridns", "wan_ipv4_snddns",
                   "lan_macaddr", "lan_ipv4_ipaddr", "lan_ipv4_netmask"),
        ).items():
            print(f"  {k:22s} {v}")

        # The DHCP pool is not in the typed dataclass; ask the router directly.
        print("\n=== raw DHCP server settings ===")
        for path, data in (
            ("admin/dhcps?form=setting", "operation=read"),
            ("admin/dhcps?form=settings", "operation=read"),
        ):
            try:
                res = client.request(path, data, ignore_errors=True)
                if res:
                    print(f"  [{path}]")
                    for k, v in (res.get("data") or res).items():
                        print(f"    {k}: {v}")
                    break
            except Exception as exc:
                print(f"  [{path}] -> {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
