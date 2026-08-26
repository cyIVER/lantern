#!/usr/bin/env python3
"""Build a cloud-init NoCloud seed ISO.

cloud-init finds its configuration by looking for a filesystem labelled
CIDATA holding files called `user-data`, `meta-data` and (optionally)
`network-config`. Those names are not 8.3, so a plain ISO 9660 image would
present them as USERDATA.;1 and cloud-init would find nothing at all -- the VM
boots to a login prompt with no user, no key and no network. The image
therefore carries Joliet and Rock Ridge extensions so the real names survive.

Normally this is one `cloud-localds` call, but that needs genisoimage, which
needs root to install. pycdlib installs into ~/.local without root, so this
works on a machine you cannot get sudo on.

    python3 make-seed-iso.py OUT.iso user-data meta-data [network-config]
"""
import io
import os
import sys

try:
    import pycdlib
except ImportError:
    sys.exit("pycdlib is missing. Install it with:\n"
             "  python3 -m pip install --user --break-system-packages pycdlib")


def iso_name(name: str) -> str:
    """An 8.3-ish ISO 9660 identifier. Only ever seen by tools, never by
    cloud-init, which reads the Joliet and Rock Ridge names instead."""
    stem = "".join(c for c in name.upper() if c.isalnum())[:8]
    return f"/{stem}.;1"


def main() -> int:
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    out, sources = sys.argv[1], sys.argv[2:]

    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3, rock_ridge="1.09", vol_ident="CIDATA")

    for path in sources:
        name = os.path.basename(path)
        with open(path, "rb") as fh:
            data = fh.read()
        if not data:
            sys.exit(f"{path} is empty -- refusing to build a seed that would "
                     f"leave cloud-init half-configured")
        iso.add_fp(io.BytesIO(data), len(data),
                   iso_path=iso_name(name),
                   rr_name=name,
                   joliet_path=f"/{name}")
        print(f"  + {name}  ({len(data)} bytes)")

    iso.write(out)
    iso.close()

    # Read it back. A seed ISO that is subtly wrong produces a VM that boots
    # fine and is simply unreachable, which is a miserable thing to debug.
    check = pycdlib.PyCdlib()
    check.open(out)
    found = sorted(c.file_identifier().decode("utf-16_be")
                   for c in check.list_children(joliet_path="/")
                   if c is not None and not c.is_dot() and not c.is_dotdot())
    check.close()

    want = sorted(os.path.basename(p) for p in sources)
    if found != want:
        sys.exit(f"verification FAILED: ISO contains {found}, expected {want}")

    print(f"  verified: {os.path.getsize(out)} bytes, label CIDATA, "
          f"names {', '.join(found)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
