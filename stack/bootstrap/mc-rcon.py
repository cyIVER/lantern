#!/usr/bin/env python3
"""Minimal Source RCON client for the Minecraft server.

Standalone and dependency-free so validation never needs the control UI running.

Unlike CS2 -- which never echoes the sentinel packet back, so ui/app/rcon.py has
to fall back on an idle timer -- Minecraft implements RCON properly: responses
arrive in order and the request id is reflected. That makes a plain
request/response exchange reliable here.

    printf '%s' "$RCON_PASSWORD" |
      python3 mc-rcon.py --password-stdin <host> <port> "list"

The legacy positional-password form remains available for interactive use, but
automation should use ``--password-stdin`` so the password is not visible in a
process listing.
"""
from __future__ import annotations

import socket
import struct
import sys

SERVERDATA_AUTH = 3
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_AUTH_RESPONSE = 2


class RconError(Exception):
    pass


def _pack(req_id: int, kind: int, body: str) -> bytes:
    payload = struct.pack("<ii", req_id, kind) + body.encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


def _read_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise RconError("connection closed mid-packet")
        buf += chunk
    return buf


def _read_packet(sock: socket.socket) -> tuple[int, int, str]:
    (length,) = struct.unpack("<i", _read_exact(sock, 4))
    if not 10 <= length <= 4_194_304:
        raise RconError(f"implausible packet length {length}")
    payload = _read_exact(sock, length)
    req_id, kind = struct.unpack("<ii", payload[:8])
    return req_id, kind, payload[8:-2].decode("utf-8", "replace")


def execute(host: str, port: int, password: str, command: str, timeout: float = 10.0) -> str:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)

        sock.sendall(_pack(1, SERVERDATA_AUTH, password))
        req_id, kind, _ = _read_packet(sock)
        # Some servers send an empty RESPONSE_VALUE before the auth result.
        if kind != SERVERDATA_AUTH_RESPONSE:
            req_id, kind, _ = _read_packet(sock)
        if req_id == -1:
            raise RconError("authentication failed (wrong password)")

        sock.sendall(_pack(2, SERVERDATA_EXECCOMMAND, command))
        _, _, body = _read_packet(sock)
        return body


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["--password-stdin"]:
        if len(args) < 4:
            print(__doc__.strip(), file=sys.stderr)
            return 2
        host, port_text = args[1], args[2]
        password = sys.stdin.read().rstrip("\r\n")
        command = " ".join(args[3:])
    elif len(args) >= 4:
        host, port_text, password = args[0], args[1], args[2]
        command = " ".join(args[3:])
    else:
        print(__doc__.strip(), file=sys.stderr)
        return 2

    try:
        port = int(port_text)
    except ValueError:
        print("rcon: port must be an integer", file=sys.stderr)
        return 2
    if not 1 <= port <= 65_535 or not password or not command:
        print("rcon: host, port, password, and command are required", file=sys.stderr)
        return 2
    try:
        print(execute(host, port, password, command))
    except (RconError, OSError) as exc:
        print(f"rcon: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
