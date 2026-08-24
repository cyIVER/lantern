#!/usr/bin/env python3
"""Minimal Source RCON client for the Minecraft server.

Standalone and dependency-free so validation never needs the control UI running.

Unlike CS2 -- which never echoes the sentinel packet back, so ui/app/rcon.py has
to fall back on an idle timer -- Minecraft implements RCON properly: responses
arrive in order and the request id is reflected. That makes a plain
request/response exchange reliable here.

    python3 mc-rcon.py <host> <port> <password> "list"
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
    if len(sys.argv) < 5:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    host, port, password, command = sys.argv[1], int(sys.argv[2]), sys.argv[3], " ".join(sys.argv[4:])
    try:
        print(execute(host, port, password, command))
    except (RconError, OSError) as exc:
        print(f"rcon: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
