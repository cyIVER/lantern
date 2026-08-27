"""Interactive creation of the secret-mounted named administrator directory."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import tempfile
from pathlib import Path

from argon2 import PasswordHasher

from .identity import JsonFileIdentityDirectory

MIN_ADMIN_PASSWORD_CHARS = 16


def write_directory(path: Path, identities: list[tuple[str, str]], passwords: list[str]) -> None:
    if len(identities) != len(passwords) or not identities:
        raise ValueError("each identity requires one password")
    if any(
        not isinstance(password, str)
        or len(password) < MIN_ADMIN_PASSWORD_CHARS
        or len(password) > 1024
        for password in passwords
    ):
        raise ValueError(
            f"administrator passwords must be {MIN_ADMIN_PASSWORD_CHARS}-1024 characters"
        )
    hasher = PasswordHasher()
    document = {
        "version": 1,
        "users": [
            {
                "username": username,
                "password_hash": hasher.hash(password),
                "role": "admin",
                "upstream_alias": alias,
                "credential_version": 1,
            }
            for (username, alias), password in zip(identities, passwords, strict=True)
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            json.dump(document, output, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o640)
        # Validate the complete file through the production adapter before it
        # replaces an existing credential directory.
        JsonFileIdentityDirectory(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "identity",
        nargs="+",
        help="portal username and Pelican alias as username:alias",
    )
    args = parser.parse_args()
    identities: list[tuple[str, str]] = []
    passwords: list[str] = []
    for item in args.identity:
        username, separator, alias = item.partition(":")
        if not separator:
            parser.error(f"identity must use username:alias form: {item}")
        first = getpass.getpass(f"Password for {username}: ")
        second = getpass.getpass(f"Confirm password for {username}: ")
        if not first or first != second:
            parser.error(f"passwords for {username} did not match")
        if len(first) < MIN_ADMIN_PASSWORD_CHARS:
            parser.error(
                f"password for {username} must contain at least "
                f"{MIN_ADMIN_PASSWORD_CHARS} characters"
            )
        identities.append((username, alias))
        passwords.append(first)
    write_directory(args.output, identities, passwords)
    print(f"Wrote {len(identities)} named administrator records to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
