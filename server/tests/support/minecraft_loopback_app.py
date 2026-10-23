"""Opt-in test-only ASGI graph for real Java/Python loopback authentication."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

# Match the repository test bootstrap import order without constructing runtime.
import conftest  # noqa: F401
from fastapi import FastAPI

from src.adapter.minecraft import control_auth
from src.adapter.minecraft import current_roles
from src.adapter.minecraft import transport as transport_module
from src.adapter.minecraft.control_auth import ControlAuthContext
from src.adapter.minecraft.transport import AllowedPeer, MinecraftAuthenticatedTransport
from src.web import minecraft as minecraft_module
from src.web.minecraft import create_minecraft_router

SERVER_KEY = bytes(range(32))
CLIENT_KEY = bytes(range(32, 64))


def _context(role: str, install: str) -> ControlAuthContext:
    return ControlAuthContext(
        peer_role=role,
        peer_install_id=install,
        hub_install_id="loopback-hub",
        key_id=f"loopback-{role.lower()}",
        protocol="luosandbox.minecraft.v1",
        mod_hash=b"m" * 32,
        config_hash=b"c" * 32,
        binding_hash=b"b" * 32,
    )


def _utc_now() -> datetime:
    value = datetime.now(timezone.utc)
    return value.replace(microsecond=value.microsecond // 1000 * 1000)


transport = MinecraftAuthenticatedTransport(
    peers=(
        AllowedPeer(_context("SERVER_AUTHORITY", "loopback-server"), SERVER_KEY),
        AllowedPeer(_context("CLIENT_CONTROLLER", "loopback-client"), CLIENT_KEY),
    ),
    hub_install_id="loopback-hub",
    monotonic_now=__import__("time").monotonic,
    utc_now=_utc_now,
)

app = FastAPI()
router, capability = create_minecraft_router(enabled=True, transport=transport)
app.include_router(router)


@app.get("/minecraft/v1/test-status")
async def status() -> dict[str, object]:
    return {
        "enabled": capability.enabled,
        "routeRegistered": capability.route_registered,
        "prewireFrameLimitRequired": capability.prewire_frame_limit_required,
        "activeRoleCount": len(transport.active_roles),
        "loadedModuleHashes": {
            "python:src/adapter/minecraft/transport.py": sha256(
                Path(transport_module.__file__).read_bytes()
            ).hexdigest(),
            "python:src/adapter/minecraft/control_auth.py": sha256(
                Path(control_auth.__file__).read_bytes()
            ).hexdigest(),
            "python:src/adapter/minecraft/current_roles.py": sha256(
                Path(current_roles.__file__).read_bytes()
            ).hexdigest(),
            "python:src/web/minecraft.py": sha256(Path(minecraft_module.__file__).read_bytes()).hexdigest(),
            "python:tests/support/minecraft_loopback_app.py": sha256(Path(__file__).read_bytes()).hexdigest(),
        },
        "loadedModulePaths": {
            "python:src/adapter/minecraft/transport.py": str(Path(transport_module.__file__).resolve()),
            "python:src/adapter/minecraft/control_auth.py": str(Path(control_auth.__file__).resolve()),
            "python:src/adapter/minecraft/current_roles.py": str(Path(current_roles.__file__).resolve()),
            "python:src/web/minecraft.py": str(Path(minecraft_module.__file__).resolve()),
            "python:tests/support/minecraft_loopback_app.py": str(Path(__file__).resolve()),
        },
    }
