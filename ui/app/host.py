"""Health of the machine LANtern runs on.

Everything here is read from inside a container, which sounds like it would
give container numbers and mostly does not: /proc/meminfo, /proc/loadavg and
/proc/uptime are the host's, because this container is not memory-limited and
shares the host PID namespace's view of those files. That is worth knowing
before trusting them -- if the ui service ever gains a `mem_limit`, the memory
figure here silently becomes the container's, not the box's.

Disk is the one that genuinely cannot be read that way: the container's own /
is the image, not the host's root filesystem. So it is measured through
/volumes, which is bind-mounted from /var/lib/pelican/volumes on the host and
therefore reports the filesystem the game servers actually fill up.

Container counts and the OS description come from the Docker socket, which is
already mounted for starting and stopping Stardew.
"""

from __future__ import annotations

import os
import pathlib
from typing import Any

import httpx

DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")

# Bind-mounted from the host, so statvfs here describes the host's disk.
DISK_PATH = os.environ.get("HOST_DISK_PATH", "/volumes")


def _read(path: str) -> str:
    try:
        return pathlib.Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def _meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    for line in _read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[key] = int(parts[0])  # kB
    return out


def _uptime_seconds() -> float:
    raw = _read("/proc/uptime").split()
    try:
        return float(raw[0])
    except (IndexError, ValueError):
        return 0.0


def _loadavg() -> list[float]:
    parts = _read("/proc/loadavg").split()
    try:
        return [float(x) for x in parts[:3]]
    except ValueError:
        return [0.0, 0.0, 0.0]


def _disk() -> dict[str, Any]:
    try:
        st = os.statvfs(DISK_PATH)
    except OSError:
        return {"available": False}
    total = st.f_blocks * st.f_frsize
    # f_bavail, not f_bfree: the difference is the root-reserved slice, which
    # nothing here can actually use.
    free = st.f_bavail * st.f_frsize
    return {
        "available": True,
        "total_gb": round(total / 1e9, 1),
        "used_gb": round((total - free) / 1e9, 1),
        "free_gb": round(free / 1e9, 1),
        "percent": round((total - free) / total * 100, 1) if total else 0.0,
    }


async def _docker() -> dict[str, Any]:
    try:
        transport = httpx.AsyncHTTPTransport(uds=DOCKER_SOCK)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://docker", timeout=10) as c:
            info = (await c.get("/info")).json()
            containers = (await c.get("/containers/json?all=1")).json()
    except Exception as exc:
        return {"available": False, "detail": str(exc)}

    running = sum(1 for x in containers if x.get("State") == "running")
    return {
        "available": True,
        "hostname": info.get("Name"),
        "os": info.get("OperatingSystem"),
        "kernel": info.get("KernelVersion"),
        "docker": info.get("ServerVersion"),
        "cpus": info.get("NCPU"),
        "containers_running": running,
        "containers_total": len(containers),
    }


async def snapshot() -> dict[str, Any]:
    mem = _meminfo()
    total_kb = mem.get("MemTotal", 0)
    # MemAvailable, not MemFree: free memory excludes cache the kernel would
    # hand back on demand, and reads alarmingly low on a healthy machine.
    avail_kb = mem.get("MemAvailable", mem.get("MemFree", 0))
    docker = await _docker()
    cores = docker.get("cpus") or os.cpu_count() or 1
    load = _loadavg()

    return {
        "uptime_seconds": int(_uptime_seconds()),
        "load": load,
        # Load relative to core count is the number worth showing: 4.0 is idle
        # on 12 cores and on fire on 2.
        "load_per_core": round(load[0] / cores, 2) if cores else 0.0,
        "cores": cores,
        "memory": {
            "total_gb": round(total_kb / 1048576, 1),
            "used_gb": round((total_kb - avail_kb) / 1048576, 1),
            "available_gb": round(avail_kb / 1048576, 1),
            "percent": round((total_kb - avail_kb) / total_kb * 100, 1) if total_kb else 0.0,
        },
        "disk": _disk(),
        "docker": docker,
    }
