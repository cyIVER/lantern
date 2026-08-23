"""Shared router session handling and safe serialization.

Two invariants this module exists to enforce:

1. Sessions are short-lived. The TP-Link web interface permits exactly ONE
   logged-in user at a time, so holding a session would lock the human out of
   their own router. Every call authorizes, acts, and logs out.

2. Credentials never leave. get_wifi() returns psk_key and portal_password in
   plaintext; those are redacted before anything is handed back to an agent.
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from tplinkrouterc6u import AuthorizeError, Connection, TplinkRouterProvider

import credentials

HOST = os.environ.get("LANTERN_ROUTER_HOST", "http://192.168.0.1")
TIMEOUT = int(os.environ.get("LANTERN_ROUTER_TIMEOUT", "15"))

REDACTED = "<redacted by LANtern>"
SECRET_FIELDS = {"psk_key", "portal_password", "password", "psk"}

# Serialize access so two concurrent tool calls cannot fight over the router's
# single permitted session.
_lock = threading.Lock()


class RouterUnavailable(RuntimeError):
    """Raised with an actionable message rather than a raw library traceback."""


@contextmanager
def session() -> Iterator[Any]:
    password = credentials.get_password()
    if not password:
        raise RouterUnavailable(
            "No router password is stored. In the LANtern repo run:\n"
            "    cd mcp/router && uv run set_password.py\n"
            "You type it into a hidden prompt; it goes to Windows Credential "
            "Manager and is never shown to an agent."
        )

    with _lock:
        client = TplinkRouterProvider.get_client(HOST, password, timeout=TIMEOUT)
        try:
            client.authorize()
        except AuthorizeError as exc:
            raise RouterUnavailable(
                f"Could not log in to {HOST}. Two usual causes:\n"
                "  1. You are logged into the router's web UI in a browser. It "
                "allows only ONE session at a time -- log out and retry.\n"
                "  2. The stored password is wrong: uv run set_password.py --force"
            ) from exc
        except Exception as exc:
            raise RouterUnavailable(f"Could not reach the router at {HOST}: {exc}") from exc

        try:
            yield client
        finally:
            try:
                client.logout()
            except Exception:
                pass  # Best effort; the session expires on its own.


def scrub(value: Any) -> Any:
    """Recursively drop secret-bearing keys from anything we return."""
    if isinstance(value, dict):
        return {
            k: (REDACTED if k in SECRET_FIELDS else scrub(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    return value


def dump(obj: Any, *, props: tuple[str, ...] = ()) -> dict:
    """Serialize a library dataclass.

    Underscore-prefixed fields (_macaddr, _ipaddr) have formatted public
    property equivalents, so prefer those and hide the raw backing fields.
    """
    out: dict[str, Any] = {}
    for key, val in vars(obj).items():
        if key.startswith("_"):
            continue
        out[key] = val.name if isinstance(val, Connection) else val
    for name in props:
        if hasattr(obj, name):
            out[name] = getattr(obj, name)
    return scrub(out)
