import asyncio
from datetime import datetime, timezone

import pytest

import src.domain.agent as d
from src.adapter.minecraft.control_auth import AuthenticatedHandshake, ControlAuthContext
from src.adapter.minecraft.control_binding import (
    ActiveBinding,
    ActiveBindingSnapshot,
    AuthenticatedRoleConnection,
    BindingTuple,
)
from src.adapter.minecraft.control_session import ControlSessionError, MinecraftControlSession
from src.domain.agent.minecraft import MinecraftActionRequest, MinecraftBoundScope, MinecraftIntent

NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)
BODY = "10000000-0000-0000-0000-000000000001"
BOOT = "20000000-0000-0000-0000-000000000002"
GAME = "30000000-0000-0000-0000-000000000003"


class Coordinator:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def active_snapshot(self):
        return self.snapshot


class Sender:
    def __init__(self):
        self.sent = []

    async def send(self, envelope):
        self.sent.append(envelope)

    async def revoke(self, requirement):
        pass


class BlockingSender(Sender):
    def __init__(self):
        super().__init__()
        self.revoke_entered = asyncio.Event()
        self.release = asyncio.Event()

    async def revoke(self, requirement):
        self.revoke_entered.set()
        await self.release.wait()


def server(connection="server-a"):
    return AuthenticatedRoleConnection(
        connection_id=connection,
        handshake=AuthenticatedHandshake(
            context=ControlAuthContext(
                peer_role="SERVER_AUTHORITY",
                peer_install_id="server-install",
                hub_install_id="hub",
                key_id="key",
                protocol="luosandbox.minecraft.v1",
                mod_hash=b"m" * 32,
                config_hash=b"c" * 32,
                binding_hash=b"b" * 32,
            ),
            challenge_id="40000000-0000-0000-0000-000000000004",
            ready_proof="proof",
        ),
    )


def binding(session, epoch):
    return BindingTuple(
        actor_id="actor",
        world_id="world",
        body_id=BODY,
        session_id=session,
        session_epoch=epoch,
        server_boot_id=BOOT,
        game_connection_id=GAME,
    )


def snapshot(generation, selected, selected_server):
    return ActiveBindingSnapshot(
        generation,
        ActiveBinding(selected, selected_server.connection_id, "client"),
        selected_server,
    )


def observation(selected, revision):
    return d.MinecraftObserved(
        stimulus_id=f"observation-{revision}",
        schema_version=1,
        occurred_at=NOW,
        source=d.StimulusSource.WORLD,
        target_character_ids=("luotianyi",),
        user_id=None,
        ephemeral=True,
        observation_id=f"observation-{revision}",
        body_id=selected.body_id,
        session_id=selected.session_id,
        session_epoch=selected.session_epoch,
        server_boot_id=selected.server_boot_id,
        server_revision=revision,
        server_tick=revision,
        state=d.MinecraftBodyState(
            dimension_id="minecraft:overworld",
            position=d.MinecraftPosition(x=0.0, y=64.0, z=0.0),
            yaw=0.0,
            pitch=0.0,
        ),
        capabilities=d.MinecraftCapabilities(
            query=d.MinecraftCapabilityAvailability.AVAILABLE,
            public_say=d.MinecraftCapabilityAvailability.AVAILABLE,
            look=d.MinecraftCapabilityAvailability.AVAILABLE,
            bounded_walk=d.MinecraftCapabilityAvailability.AVAILABLE,
            stop=d.MinecraftCapabilityAvailability.AVAILABLE,
        ),
        evidence_refs=(),
        untrusted_text=(),
    )


@pytest.mark.asyncio
async def test_composition_generation_rebind_blocks_old_facade_and_builds_new_port_graph():
    first, second = (
        binding("50000000-0000-0000-0000-000000000005", 1),
        binding("60000000-0000-0000-0000-000000000006", 2),
    )
    coordinator = Coordinator(snapshot(1, first, server()))
    sender = Sender()
    owner = MinecraftControlSession(
        enabled=True,
        coordinator=coordinator,
        sender=sender,
        monotonic_now=lambda: 10.0,
        utc_now=lambda: NOW,
        admission_timeout_seconds=0.01,
    )
    runtime_port = await owner.activate(observation(first, 1))
    old = owner._current
    coordinator.snapshot = None
    assert owner.active_context is None
    with pytest.raises(ControlSessionError):
        await runtime_port.submit(
            MinecraftActionRequest(
                request_id="old",
                scope=MinecraftBoundScope(world_id="world", body_id=BODY, session_epoch=1),
                intent=MinecraftIntent.QUERY,
                basis_observation_id=None,
            )
        )
    coordinator.snapshot = snapshot(2, second, server("server-b"))
    await owner.activate(observation(second, 2))
    assert owner._current.ledger is not old.ledger
    assert owner._current.port is not old.port
    assert owner.active_context.binding == second
    assert old.port._active_context() is None


@pytest.mark.asyncio
async def test_cancelled_synchronize_publishes_retired_history_before_port_cleanup_wait():
    selected = binding("50000000-0000-0000-0000-000000000005", 1)
    coordinator = Coordinator(snapshot(1, selected, server()))
    sender = BlockingSender()
    owner = MinecraftControlSession(
        enabled=True,
        coordinator=coordinator,
        sender=sender,
        monotonic_now=lambda: 10.0,
        utc_now=lambda: NOW,
        admission_timeout_seconds=5,
    )
    await owner.activate(observation(selected, 1))
    old = owner._current
    submit = asyncio.create_task(
        owner.runtime_port.submit(
            MinecraftActionRequest(
                request_id="pending",
                scope=MinecraftBoundScope(world_id="world", body_id=BODY, session_epoch=1),
                intent=MinecraftIntent.QUERY,
                basis_observation_id=None,
            )
        )
    )
    while not sender.sent:
        await asyncio.sleep(0)
    coordinator.snapshot = None
    retire = asyncio.create_task(owner.synchronize())
    await sender.revoke_entered.wait()
    retire.cancel()
    await retire
    assert owner._current is None
    assert owner._retired == [old]
    assert old.current is False and old.port._active_context() is None
    assert (await submit).admission.value == "unknown"
