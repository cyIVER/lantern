"""Minimal async Source RCON client.

Pelican's command endpoint is fire-and-forget -- it pushes a command to the
server's stdin and returns nothing -- so anything that needs to *read* a reply
(the player roster, cvar values) has to speak RCON directly.

A fresh connection is made per call. CS2 drops idle RCON sessions and a pooled
connection would fail intermittently in ways that are annoying to debug; the
handshake costs a millisecond on a LAN.
"""
from __future__ import annotations

import asyncio
import struct

SERVERDATA_AUTH = 3
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_RESPONSE_VALUE = 0


class RconError(RuntimeError):
    pass


def _encode(req_id: int, req_type: int, body: str) -> bytes:
    payload = struct.pack("<ii", req_id, req_type) + body.encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


async def _read_packet(reader: asyncio.StreamReader) -> tuple[int, int, str]:
    raw_len = await reader.readexactly(4)
    (length,) = struct.unpack("<i", raw_len)
    if not 10 <= length <= 4096 * 16:
        raise RconError(f"implausible RCON packet length {length}")
    payload = await reader.readexactly(length)
    req_id, req_type = struct.unpack("<ii", payload[:8])
    body = payload[8:-2].decode("utf-8", errors="replace")
    return req_id, req_type, body


async def execute(host: str, port: int, password: str, command: str,
                  timeout: float = 6.0) -> str:
    """Authenticate, run one command, return its output."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout)
    except (OSError, asyncio.TimeoutError) as exc:
        raise RconError(f"cannot reach RCON at {host}:{port} ({exc})") from exc

    try:
        writer.write(_encode(1, SERVERDATA_AUTH, password))
        await writer.drain()

        # The server may emit an empty RESPONSE_VALUE before the auth result.
        req_id = None
        for _ in range(2):
            req_id, req_type, _ = await asyncio.wait_for(_read_packet(reader), timeout)
            if req_type == SERVERDATA_AUTH_RESPONSE:
                break
        if req_id == -1:
            raise RconError("RCON authentication failed (wrong password)")

        writer.write(_encode(2, SERVERDATA_EXECCOMMAND, command))
        await writer.drain()

        # No sentinel packet here. The classic trick -- following the command
        # with an empty RESPONSE_VALUE and reading until its echo returns -- does
        # NOT work on CS2: it never echoes it back, and sending one makes CS2
        # return nothing at all. Instead: wait the full timeout for the first
        # packet, then drain any continuation packets with a short idle window.
        chunks: list[str] = []
        try:
            _rid, _rtype, body = await asyncio.wait_for(_read_packet(reader), timeout)
            chunks.append(body)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError):
            return ""

        while True:
            try:
                _rid, _rtype, body = await asyncio.wait_for(_read_packet(reader), 0.4)
            except (asyncio.IncompleteReadError, asyncio.TimeoutError):
                break
            chunks.append(body)
        return "".join(chunks)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
