"""Credential access for the LANtern router MCP server.

The router password is stored in the OS keyring -- on Windows that is
Credential Manager, encrypted at rest with DPAPI under your user account.

It is deliberately NOT kept in .env, in the repo, or anywhere an agent is
likely to read or accidentally commit it. No tool in this server ever
returns it, logs it, or includes it in an error message.
"""
from __future__ import annotations

import os

import keyring

SERVICE = "lantern-router"
ACCOUNT = "admin"

# Escape hatch for headless/CI use. Keyring is strongly preferred.
ENV_VAR = "LANTERN_ROUTER_PASSWORD"


def get_password() -> str | None:
    """Return the stored router password, or None if it has not been set."""
    from_env = os.environ.get(ENV_VAR)
    if from_env:
        return from_env
    try:
        return keyring.get_password(SERVICE, ACCOUNT)
    except Exception:
        return None


def set_password(password: str) -> None:
    """Store the router admin password in the OS keyring."""
    keyring.set_password(SERVICE, ACCOUNT, password)


def clear_password() -> None:
    """Delete the stored router password from the OS keyring."""
    try:
        keyring.delete_password(SERVICE, ACCOUNT)
    except keyring.errors.PasswordDeleteError:
        pass


def is_configured() -> bool:
    """Check whether a router password is available from the environment or keyring."""
    return get_password() is not None
