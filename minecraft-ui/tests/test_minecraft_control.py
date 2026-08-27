import asyncio
from collections import deque

import httpx
import pytest

from app.minecraft_control import (
    InMemoryMinecraftControl,
    LanternControlHttpAdapter,
    MinecraftControlError,
)


def test_in_memory_control_preserves_confirmation_before_conflicting_start() -> None:
    async def scenario() -> None:
        control = InMemoryMinecraftControl(conflicting_game="Stardew Valley")

        challenge = await control.power("start")
        assert challenge.outcome == "confirmation_required"
        assert challenge.effects == ("Stop Stardew Valley",)
        assert control.actions == []

        result = await control.power("start", confirmed=True)
        assert result.outcome == "done"
        assert result.state.state == "running"
        assert control.actions == ["start"]

    asyncio.run(scenario())


def test_http_adapter_translates_lantern_confirmation_without_leaking_response() -> None:
    async def upstream(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                409,
                json={
                    "detail": {
                        "needs_confirm": True,
                        "message": "Starting Minecraft will shut down Stardew Valley.",
                        "would_stop_labels": ["Stardew Valley"],
                    }
                },
            )
        return httpx.Response(
            200,
            json={"servers": [{"id": "minecraft", "state": "stopped", "available": True}]},
        )

    async def scenario() -> None:
        control = LanternControlHttpAdapter(
            "http://ui:8090", transport=httpx.MockTransport(upstream)
        )
        try:
            result = await control.power("start")
        finally:
            await control.aclose()
        assert result.outcome == "confirmation_required"
        assert result.effects == ("Stop Stardew Valley",)

    asyncio.run(scenario())


def test_restart_waits_for_stopped_state_before_starting() -> None:
    states = deque(["running", "stopping", "stopped", "running"])
    requests: list[str] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(f"{request.method} {request.url.path}")
        if request.method == "GET":
            state = states.popleft()
            return httpx.Response(
                200,
                json={"servers": [{"id": "minecraft", "state": state, "available": True}]},
            )
        return httpx.Response(200, json={"ok": True})

    async def scenario() -> None:
        control = LanternControlHttpAdapter(
            "http://ui:8090",
            transport=httpx.MockTransport(upstream),
            poll_seconds=0,
        )
        try:
            result = await control.power("restart", confirmed=True)
        finally:
            await control.aclose()
        assert result.outcome == "done"
        assert result.state.state == "running"

    asyncio.run(scenario())

    stop_index = requests.index("POST /api/servers/minecraft/stop")
    start_index = requests.index("POST /api/servers/minecraft/start")
    assert stop_index < start_index
    assert requests[stop_index + 1 : start_index].count("GET /api/servers") >= 2


def test_restart_refuses_to_start_when_shutdown_never_finishes() -> None:
    async def upstream(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"servers": [{"id": "minecraft", "state": "stopping", "available": True}]},
            )
        return httpx.Response(200, json={"ok": True})

    async def scenario() -> None:
        control = LanternControlHttpAdapter(
            "http://ui:8090",
            transport=httpx.MockTransport(upstream),
            restart_timeout_seconds=0,
            poll_seconds=0,
        )
        try:
            with pytest.raises(MinecraftControlError) as raised:
                await control.power("restart", confirmed=True)
        finally:
            await control.aclose()
        assert raised.value.code == "dependency_timeout"

    asyncio.run(scenario())
