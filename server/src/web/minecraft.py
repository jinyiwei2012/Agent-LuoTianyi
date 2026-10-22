"""Explicit opt-in FastAPI router factory for disabled Minecraft transport."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Callable

from fastapi import APIRouter, WebSocket

from src.adapter.minecraft.transport import MinecraftAuthenticatedTransport


@dataclass(frozen=True, slots=True)
class MinecraftTransportCapability:
    enabled: bool
    route_registered: bool
    prewire_frame_limit_required: bool = True


def create_minecraft_router(
    *,
    enabled: bool = False,
    transport: MinecraftAuthenticatedTransport | None = None,
    peer_is_loopback: Callable[[WebSocket], bool] | None = None,
) -> tuple[APIRouter, MinecraftTransportCapability]:
    """Create an unmounted router; disabled mode registers no endpoint.

    The ASGI server must separately enforce a 4096-byte WebSocket frame cap,
    because a route only sees text after the server has allocated it.
    """
    if type(enabled) is not bool:
        raise TypeError("Minecraft transport enabled must be bool")
    router = APIRouter()
    if not enabled:
        return router, MinecraftTransportCapability(False, False)
    if transport is None:
        raise ValueError("enabled Minecraft transport requires provisioned authentication")
    validator = peer_is_loopback or _is_loopback

    @router.websocket("/minecraft/v1/ws")
    async def minecraft_control_socket(websocket: WebSocket) -> None:
        protocols = {
            item.strip() for item in websocket.headers.get("sec-websocket-protocol", "").split(",") if item.strip()
        }
        host = websocket.headers.get("host", "").split(":", 1)[0].strip("[]")
        if (
            "luosandbox.minecraft.v1" not in protocols
            or host not in {"127.0.0.1", "::1", "localhost"}
            or not validator(websocket)
        ):
            await websocket.close(code=1008, reason="LOCAL_ONLY")
            return
        await transport.handle(websocket)

    return router, MinecraftTransportCapability(True, True)


def _is_loopback(websocket: WebSocket) -> bool:
    host = None if websocket.client is None else websocket.client.host
    try:
        return host is not None and ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
