"""Store the router password in Windows Credential Manager.

Run this yourself -- it prompts with a hidden input, so the password is never
typed into a chat, never passed as an argument, and never enters agent context:

    uv run set_password.py
"""
from __future__ import annotations

import getpass
import sys

import credentials


def main() -> int:
    if "--clear" in sys.argv:
        credentials.clear_password()
        print("Cleared stored router password.")
        return 0

    if credentials.is_configured() and "--force" not in sys.argv:
        print("A router password is already stored. Use --force to replace it.")
        return 0

    print(f"Storing under service '{credentials.SERVICE}' / account '{credentials.ACCOUNT}'.")
    password = getpass.getpass("Router admin password (input hidden): ")
    if not password:
        print("Aborted -- empty password.")
        return 1

    confirm = getpass.getpass("Confirm: ")
    if password != confirm:
        print("Aborted -- passwords did not match.")
        return 1

    credentials.set_password(password)
    print("Stored. Verify with:  uv run verify.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
