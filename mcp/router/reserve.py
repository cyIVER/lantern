"""Add or inspect DHCP reservations on the router.

Dry-run by default. Nothing is written without --apply.

    uv run reserve.py                                  # list current reservations
    uv run reserve.py --mac A0-36-BC-BA-5A-C3 --ip 192.168.0.115 --comment "LANtern host"
    uv run reserve.py --mac ... --ip ... --apply       # actually write it
"""
from __future__ import annotations

import argparse
import re

import router as rt


def normalize_mac(mac: str, sample: str | None) -> str:
    """Match the separator style the router already uses, so we don't create
    a duplicate entry that differs only in formatting."""
    hexes = re.sub(r"[^0-9A-Fa-f]", "", mac).upper()
    if len(hexes) != 12:
        raise SystemExit(f"Not a valid MAC address: {mac!r}")
    pairs = [hexes[i:i + 2] for i in range(0, 12, 2)]
    sep = "-"
    if sample and ":" in sample:
        sep = ":"
    return sep.join(pairs)


def show(reservations, leases) -> None:
    """Display current DHCP reservations and active leases."""
    print(f"\nCurrent reservations ({len(reservations)}):")
    if not reservations:
        print("   (none)")
    for r in reservations:
        flag = "" if r.enabled else "  [disabled]"
        print(f"   {r.macaddr:20s} -> {r.ipaddr:15s} {r.hostname or ''}{flag}")

    print(f"\nActive DHCP leases ({len(leases)}):")
    for l in leases:
        print(f"   {l.macaddr:20s} -> {l.ipaddr:15s} {l.hostname or ''}")


def main() -> int:
    """List existing DHCP reservations or add a new one (dry-run by default)."""
    p = argparse.ArgumentParser(description="Inspect or add a DHCP reservation.")
    p.add_argument("--mac")
    p.add_argument("--ip")
    p.add_argument("--comment", default="")
    p.add_argument("--apply", action="store_true", help="actually write the change")
    args = p.parse_args()

    with rt.session() as client:
        for needed in ("get_ipv4_reservations", "get_ipv4_dhcp_leases"):
            if not hasattr(client, needed):
                raise SystemExit(f"This router model does not support {needed}().")

        reservations = client.get_ipv4_reservations()
        leases = client.get_ipv4_dhcp_leases()
        show(reservations, leases)

        if not args.mac or not args.ip:
            print("\n(No --mac/--ip given, so nothing to add.)")
            return 0

        sample = reservations[0].macaddr if reservations else None
        mac = normalize_mac(args.mac, sample)

        existing = [r for r in reservations if r.macaddr.upper().replace(":", "-")
                    == mac.upper().replace(":", "-")]
        if existing:
            cur = existing[0]
            if cur.ipaddr == args.ip:
                print(f"\nAlready reserved: {mac} -> {args.ip}. Nothing to do.")
                return 0
            print(f"\nWARNING: {mac} is already reserved to {cur.ipaddr}, not {args.ip}.")
            print("Remove the old entry in the web UI first; this tool will not overwrite.")
            return 1

        clash = [r for r in reservations if r.ipaddr == args.ip]
        if clash:
            print(f"\nWARNING: {args.ip} is already reserved for {clash[0].macaddr}.")
            return 1

        print(f"\nPlanned change:  reserve {args.ip}  for  {mac}"
              + (f'  ("{args.comment}")' if args.comment else ""))

        if not args.apply:
            print("\nDRY RUN. Re-run with --apply to write it.")
            return 0

        if not hasattr(client, "add_ipv4_reservation"):
            raise SystemExit("This router model does not support add_ipv4_reservation().")

        client.add_ipv4_reservation(mac, args.ip, args.comment, True)
        print("Applied. Re-reading to confirm...")
        after = client.get_ipv4_reservations()
        ok = any(r.ipaddr == args.ip for r in after)
        print("CONFIRMED." if ok else "NOT FOUND after write -- check the web UI.")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
