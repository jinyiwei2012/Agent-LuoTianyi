from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.web.minecraft import create_minecraft_router


def test_disabled_factory_registers_no_endpoint_and_reports_prewire_requirement():
    router, capability = create_minecraft_router()
    app = FastAPI()
    app.include_router(router)
    assert capability.enabled is False
    assert capability.route_registered is False
    assert capability.prewire_frame_limit_required is True
    assert all(getattr(route, "path", None) != "/minecraft/v1/ws" for route in app.routes)


def test_enabled_factory_requires_provisioned_transport():
    try:
        create_minecraft_router(enabled=True)
    except ValueError as error:
        assert "provisioned" in str(error)
    else:
        raise AssertionError("enabled transport must require credentials")


def test_nonloopback_or_missing_subprotocol_is_rejected_before_transport():
    class Transport:
        async def handle(self, websocket):
            raise AssertionError("transport must not run")

    router, _ = create_minecraft_router(enabled=True, transport=Transport(), peer_is_loopback=lambda ws: False)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        try:
            with client.websocket_connect("/minecraft/v1/ws", subprotocols=["luosandbox.minecraft.v1"]):
                pass
        except Exception:
            pass
