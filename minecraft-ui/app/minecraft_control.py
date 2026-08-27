"""Minecraft lifecycle operations through LANtern's existing control module.

The public portal depends on the small ``MinecraftControl`` interface below.
The production adapter talks to the LANtern control UI over a private Docker
network; tests use the in-memory adapter.  The one-game-at-a-time invariant
therefore remains owned by LANtern instead of being reimplemented here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

PowerAction = Literal["start", "stop", "restart"]


@dataclass(frozen=True, slots=True)
class MinecraftState:
    state: str
    available: bool
    players: int | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class PowerOutcome:
    outcome: Literal["done", "confirmation_required"]
    state: MinecraftState
    notice: str | None = None
    confirmation_message: str | None = None
    effects: tuple[str, ...] = ()


class MinecraftControlError(RuntimeError):
    """A sanitized, stable failure crossing the Minecraft-control seam."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class MinecraftControl(Protocol):
    async def inspect(self) -> MinecraftState: ...

    async def power(self, action: PowerAction, *, confirmed: bool = False) -> PowerOutcome: ...


class LanternControlHttpAdapter:
    """Production adapter for LANtern's authoritative game-control interface."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 20.0,
        restart_timeout_seconds: float = 120.0,
        poll_seconds: float = 2.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            transport=transport,
            timeout=timeout_seconds,
            follow_redirects=False,
        )
        self._restart_timeout = restart_timeout_seconds
        self._poll_seconds = poll_seconds

    async def aclose(self) -> None:
        await self._client.aclose()

    async def inspect(self) -> MinecraftState:
        try:
            response = await self._client.get("/api/servers")
            response.raise_for_status()
            payload = response.json()
            row = next(item for item in payload.get("servers", []) if item.get("id") == "minecraft")
        except (httpx.HTTPError, ValueError, KeyError, StopIteration, TypeError) as exc:
            raise MinecraftControlError(
                "dependency_unavailable", "LANtern server status is unavailable"
            ) from exc
        return _state_from_row(row)

    async def power(self, action: PowerAction, *, confirmed: bool = False) -> PowerOutcome:
        if action == "restart":
            return await self._restart(confirmed=confirmed)
        return await self._single_power(action, confirmed=confirmed)

    async def _single_power(
        self, action: Literal["start", "stop"], *, confirmed: bool
    ) -> PowerOutcome:
        path = f"/api/servers/minecraft/{action}"
        request: dict[str, Any] = {"json": {"confirm": confirmed}} if action == "start" else {}
        try:
            response = await self._client.post(path, **request)
        except httpx.TimeoutException as exc:
            raise MinecraftControlError(
                "dependency_timeout", "LANtern did not finish the power request in time"
            ) from exc
        except httpx.HTTPError as exc:
            raise MinecraftControlError(
                "dependency_unavailable", "LANtern power control is unavailable"
            ) from exc

        if response.status_code == 409:
            detail = _safe_detail(response)
            if isinstance(detail, dict) and detail.get("needs_confirm"):
                labels = tuple(str(value) for value in detail.get("would_stop_labels", []))
                return PowerOutcome(
                    outcome="confirmation_required",
                    state=await self.inspect(),
                    confirmation_message=str(
                        detail.get("message", "Starting Minecraft will stop another game.")
                    ),
                    effects=tuple(f"Stop {label}" for label in labels),
                )
            raise MinecraftControlError("state_conflict", _safe_message(detail))
        if response.status_code >= 400:
            raise MinecraftControlError(
                "dependency_failed", f"LANtern refused the Minecraft {action} request"
            )
        return PowerOutcome(
            outcome="done",
            state=await self.inspect(),
            notice=f"Minecraft {action} request accepted",
        )

    async def _restart(self, *, confirmed: bool) -> PowerOutcome:
        current = await self.inspect()
        if current.state in {"stopped", "absent"}:
            return await self._single_power("start", confirmed=confirmed)

        stopped = await self._single_power("stop", confirmed=True)
        if stopped.outcome != "done":  # defensive: stop currently never asks for confirmation
            return stopped

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._restart_timeout
        while loop.time() < deadline:
            current = await self.inspect()
            if current.state in {"stopped", "absent"}:
                started = await self._single_power("start", confirmed=confirmed)
                if started.outcome == "done":
                    return PowerOutcome(
                        outcome="done",
                        state=started.state,
                        notice="Minecraft restart request accepted",
                    )
                return started
            await asyncio.sleep(self._poll_seconds)
        raise MinecraftControlError(
            "dependency_timeout", "Minecraft did not stop cleanly, so it was not restarted"
        )


class InMemoryMinecraftControl:
    """Deterministic adapter used by interface tests."""

    def __init__(
        self,
        *,
        state: str = "stopped",
        available: bool = True,
        conflicting_game: str | None = None,
    ) -> None:
        self.state = state
        self.available = available
        self.conflicting_game = conflicting_game
        self.actions: list[PowerAction] = []

    async def inspect(self) -> MinecraftState:
        return MinecraftState(self.state, self.available)

    async def power(self, action: PowerAction, *, confirmed: bool = False) -> PowerOutcome:
        if not self.available:
            raise MinecraftControlError("dependency_unavailable", "Minecraft is unavailable")
        if action == "start" and self.conflicting_game and not confirmed:
            return PowerOutcome(
                "confirmation_required",
                await self.inspect(),
                confirmation_message=f"Starting Minecraft will stop {self.conflicting_game}.",
                effects=(f"Stop {self.conflicting_game}",),
            )
        self.actions.append(action)
        self.conflicting_game = None
        self.state = "stopped" if action == "stop" else "running"
        return PowerOutcome("done", await self.inspect(), notice=f"Minecraft {action} complete")


def _state_from_row(row: Any) -> MinecraftState:
    if not isinstance(row, dict):
        raise TypeError("server row must be an object")
    return MinecraftState(
        state=str(row.get("state", "unknown")),
        available=bool(row.get("available", False)),
        players=row.get("players") if isinstance(row.get("players"), int) else None,
        detail=str(row["detail"]) if row.get("detail") else None,
    )


def _safe_detail(response: httpx.Response) -> Any:
    try:
        return response.json().get("detail")
    except (ValueError, AttributeError):
        return None


def _safe_message(detail: Any) -> str:
    if isinstance(detail, str) and detail:
        return detail[:240]
    return "Minecraft state changed; refresh and try again"
