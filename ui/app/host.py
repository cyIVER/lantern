"""Health of the machine LANtern runs on.

WHAT IS AND IS NOT THE HOST'S

Everything here is read from inside a container, and which numbers that gives
you is not uniform:

  /proc/stat, /proc/meminfo, /proc/loadavg, /proc/uptime
      the HOST's, because /proc is not namespaced for these and this container
      has no memory limit. If the ui service ever gains a `mem_limit`, the
      memory figure silently becomes the container's instead.

  /proc/net/dev
      the CONTAINER's. Network namespaces are per-container, so this would
      report traffic on a compose bridge rather than on the LAN. It is
      deliberately NOT reported here -- a plausible wrong number is worse than
      no number.

  disk
      the container's own / is the image, not the host's filesystem. So disk is
      measured through /volumes, which is bind-mounted from /var/lib/pelican and
      is therefore the filesystem the game servers actually fill up.

CPU IS A RATE, NOT A READING

/proc/stat gives cumulative jiffies since boot, so a single read says nothing
about current load. The first call after start has no previous sample to
compare against and returns null rather than a made-up zero; the UI shows a
dash until the second poll.
"""

from __future__ import annotations

import os
import pathlib
import time
from typing import Any

import httpx

DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")

# Bind-mounted from the host, so statvfs here describes the host's disk.
DISK_PATH = os.environ.get("HOST_DISK_PATH", "/volumes")

# Previous CPU sample, for the delta. Module-level on purpose: the rate is
# between successive polls of this endpoint.
_prev_cpu: tuple[float, int, int] | None = None


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


def _cpu_percent() -> float | None:
    """Busy percentage since the previous call, or None on the first."""
    global _prev_cpu
    line = next((l for l in _read("/proc/stat").splitlines()
                 if l.startswith("cpu ")), "")
    fields = [int(x) for x in line.split()[1:] if x.isdigit()]
    if len(fields) < 5:
        return None

    # user nice system idle iowait irq softirq steal ...
    total = sum(fields[:8])
    # iowait counts as not-busy: the CPU is available, it is the disk that is
    # holding things up, and that shows in the disk figure instead.
    idle = fields[3] + fields[4]

    prev, _prev_cpu = _prev_cpu, (time.time(), total, idle)
    if prev is None:
        return None
    d_total = total - prev[1]
    d_idle = idle - prev[2]
    if d_total <= 0:
        return None
    return round((1 - d_idle / d_total) * 100, 1)


def _uptime_seconds() -> float:
    try:
        return float(_read("/proc/uptime").split()[0])
    except (IndexError, ValueError):
        return 0.0


def _loadavg() -> list[float]:
    try:
        return [float(x) for x in _read("/proc/loadavg").split()[:3]]
    except ValueError:
        return [0.0, 0.0, 0.0]


def _disk() -> dict[str, Any]:
    try:
        st = os.statvfs(DISK_PATH)
    except OSError:
        return {"available": False}
    total = st.f_blocks * st.f_frsize
    # f_bavail, not f_bfree: the difference is the root-reserved slice, which
    # nothing here could actually use.
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

    # Name, state and how long -- enough to spot the one that is restarting in
    # a loop, which is the failure this list is here to make visible.
    rows = sorted(
        ({"name": (x.get("Names") or ["?"])[0].lstrip("/"),
          "state": x.get("State", "?"),
          "status": x.get("Status", ""),
          "image": (x.get("Image") or "").split("@")[0]}
         for x in containers),
        key=lambda r: (r["state"] != "running", r["name"]),
    )

    return {
        "available": True,
        "hostname": info.get("Name"),
        "os": info.get("OperatingSystem"),
        "kernel": info.get("KernelVersion"),
        "docker": info.get("ServerVersion"),
        "cpus": info.get("NCPU"),
        "images": info.get("Images"),
        "containers_running": sum(1 for r in rows if r["state"] == "running"),
        "containers_total": len(rows),
        "containers": rows,
    }


async def snapshot() -> dict[str, Any]:
    mem = _meminfo()
    total_kb = mem.get("MemTotal", 0)
    # MemAvailable, not MemFree: free memory excludes cache the kernel hands
    # back on demand, and reads alarmingly low on a perfectly healthy machine.
    avail_kb = mem.get("MemAvailable", mem.get("MemFree", 0))
    cached_kb = mem.get("Cached", 0) + mem.get("SReclaimable", 0)
    swap_total_kb = mem.get("SwapTotal", 0)
    swap_free_kb = mem.get("SwapFree", 0)

    docker = await _docker()
    cores = docker.get("cpus") or os.cpu_count() or 1
    load = _loadavg()
    gb = 1048576  # kB -> GiB

    return {
        "uptime_seconds": int(_uptime_seconds()),
        "cores": cores,
        "cpu_percent": _cpu_percent(),
        "load": load,
        # Load relative to core count is the number worth showing: 4.0 is idle
        # on twelve cores and on fire on two.
        "load_per_core": round(load[0] / cores, 2) if cores else 0.0,
        "memory": {
            "total_gb": round(total_kb / gb, 1),
            "used_gb": round((total_kb - avail_kb) / gb, 1),
            "available_gb": round(avail_kb / gb, 1),
            "cached_gb": round(cached_kb / gb, 1),
            "percent": round((total_kb - avail_kb) / total_kb * 100, 1) if total_kb else 0.0,
        },
        "swap": {
            "total_gb": round(swap_total_kb / gb, 1),
            "used_gb": round((swap_total_kb - swap_free_kb) / gb, 1),
            "percent": round((swap_total_kb - swap_free_kb) / swap_total_kb * 100, 1)
                       if swap_total_kb else 0.0,
        },
        "disk": _disk(),
        "docker": docker,
    }
