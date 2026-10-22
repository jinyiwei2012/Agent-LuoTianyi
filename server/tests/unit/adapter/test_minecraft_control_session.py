import asyncio
import json
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
from src.adapter.minecraft.wire import decode_message
from src.domain.agent.minecraft import (
    MinecraftActionRequest,
    MinecraftBoundScope,
    MinecraftIntent,
    MinecraftPortAdmission,
)

NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)
BODY = "10000000-0000-0000-0000-000000000001"
BOOT = "20000000-0000-0000-0000-000000000002"
GAME = "30000000-0000-0000-0000-000000000003"


class Clock:
    def __init__(self):
        self.value = 10.0

    def __call__(self):
        return self.value


class Sender:
    def __init__(self):
        self.sent, self.revoked = [], []

    async def send(self, envelope):
        self.sent.append(envelope)

    async def revoke(self, requirement):
        self.revoked.append(requirement)


class BlockingRevokeSender(Sender):
    def __init__(self):
        super().__init__()
        self.revoke_entered = asyncio.Event()
        self.release_revoke = asyncio.Event()

    async def revoke(self, requirement):
        self.revoked.append(requirement)
        self.revoke_entered.set()
        await self.release_revoke.wait()


class Coordinator:
    def __init__(self):
        self.snapshot = None

    def active_snapshot(self):
        return self.snapshot


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


def snapshot(generation, selected_binding, selected_server=None):
    selected_server = selected_server or server()
    return ActiveBindingSnapshot(
        generation,
        ActiveBinding(selected_binding, selected_server.connection_id, "client"),
        selected_server,
    )


def observation(selected_binding, revision=1):
    return d.MinecraftObserved(
        stimulus_id=f"observation-{revision}",
        schema_version=1,
        occurred_at=NOW,
        source=d.StimulusSource.WORLD,
        target_character_ids=("luotianyi",),
        user_id=None,
        ephemeral=True,
        observation_id=f"observation-{revision}",
        body_id=selected_binding.body_id,
        session_id=selected_binding.session_id,
        session_epoch=selected_binding.session_epoch,
        server_boot_id=selected_binding.server_boot_id,
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


def action_request(action_id="action"):
    return MinecraftActionRequest(
        request_id=action_id,
        scope=MinecraftBoundScope(world_id="world", body_id=BODY, session_epoch=1),
        intent=MinecraftIntent.QUERY,
        basis_observation_id=None,
    )


def session(enabled=True, sender=None):
    coordinator, sender, clock = Coordinator(), sender or Sender(), Clock()
    owner = MinecraftControlSession(
        enabled=enabled,
        coordinator=coordinator,
        sender=sender,
        monotonic_now=clock,
        utc_now=lambda: NOW,
        admission_timeout_seconds=0.01,
    )
    return owner, coordinator, sender, clock


def authoritative(kind, message_id, request_id, selected):
    payload = {
        "requestId": request_id,
        "stamp": {
            "serverBootId": selected.server_boot_id,
            "serverRevision": 10 if kind == "admission" else 11,
            "serverTick": 10 if kind == "admission" else 11,
            "eventId": (
                "90000000-0000-0000-0000-000000000009"
                if kind == "admission"
                else "a0000000-0000-0000-0000-00000000000a"
            ),
            "evidence": [],
        },
    }
    if kind == "admission":
        payload |= {"decision": "ACCEPTED", "reason": None}
    else:
        payload |= {
            "status": "UNKNOWN",
            "reason": "OUTCOME_UNPROVEN",
            "outcome": {"kind": "UNDETERMINED", "effect": "UNKNOWN", "state": None, "observationId": None},
        }
    value = {
        "protocol": "luosandbox.minecraft.v1",
        "type": kind,
        "actorId": selected.actor_id,
        "worldId": selected.world_id,
        "bodyId": selected.body_id,
        "sessionId": selected.session_id,
        "sessionEpoch": selected.session_epoch,
        "messageId": message_id,
        "timestamp": "2026-10-23T00:00:00.000Z",
        "payload": payload,
    }
    return decode_message(json.dumps(value, separators=(",", ":")).encode())


@pytest.mark.asyncio
async def test_default_disabled_and_one_snapshot_builds_coherent_context_and_runtime_port():
    owner, coordinator, _, _ = session(enabled=False)
    selected = binding("50000000-0000-0000-0000-000000000005", 1)
    coordinator.snapshot = snapshot(1, selected)
    with pytest.raises(ControlSessionError, match="disabled"):
        await owner.activate(observation(selected))
    owner, coordinator, _, _ = session()
    coordinator.snapshot = snapshot(1, selected)
    runtime_port = await owner.activate(observation(selected))
    context = owner.active_context
    assert runtime_port is owner.runtime_port
    assert context.binding == selected
    assert context.server_origin.connection == coordinator.snapshot.server
    assert context.observation.server_revision == 1


@pytest.mark.asyncio
async def test_disconnect_snapshot_clear_synchronously_blocks_facade_then_bounded_close_retires():
    owner, coordinator, sender, _ = session()
    selected = binding("50000000-0000-0000-0000-000000000005", 1)
    coordinator.snapshot = snapshot(1, selected)
    await owner.activate(observation(selected))
    coordinator.snapshot = None
    assert owner.active_context is None
    with pytest.raises(ControlSessionError, match="no current"):
        await owner.runtime_port.submit(action_request())
    await owner.synchronize()
    assert owner.retired_count == 1
    assert sender.sent == []


@pytest.mark.asyncio
async def test_rebind_creates_new_ledger_port_and_old_generation_never_becomes_current():
    owner, coordinator, _, _ = session()
    first = binding("50000000-0000-0000-0000-000000000005", 1)
    second = binding("60000000-0000-0000-0000-000000000006", 2)
    coordinator.snapshot = snapshot(1, first)
    await owner.activate(observation(first))
    old = owner._current
    coordinator.snapshot = snapshot(2, second, server("server-b"))
    await owner.activate(observation(second, revision=2))
    assert owner._current.port is not old.port
    assert owner._current.ledger is not old.ledger
    assert old.current is False
    assert old.port._active_context() is None
    assert owner.active_context.binding == second


@pytest.mark.asyncio
async def test_observation_must_match_same_snapshot_and_retired_history_is_bounded():
    owner, coordinator, _, clock = session()
    first = binding("50000000-0000-0000-0000-000000000005", 1)
    coordinator.snapshot = snapshot(1, first)
    wrong = binding("60000000-0000-0000-0000-000000000006", 2)
    with pytest.raises(ControlSessionError, match="does not match"):
        await owner.activate(observation(wrong))
    await owner.activate(observation(first))
    for generation in (2, 3):
        selected = binding(f"70000000-0000-0000-0000-{generation:012x}", generation)
        coordinator.snapshot = snapshot(generation, selected, server(f"server-{generation}"))
        await owner.activate(observation(selected, revision=generation))
    assert owner.retired_count == 2
    selected = binding("80000000-0000-0000-0000-000000000008", 4)
    coordinator.snapshot = snapshot(4, selected, server("server-4"))
    with pytest.raises(ControlSessionError, match="history capacity"):
        await owner.activate(observation(selected, revision=4))
    clock.value += 300
    await owner.activate(observation(selected, revision=4))
    assert owner.retired_count <= 2


@pytest.mark.asyncio
async def test_cancelled_retirement_keeps_history_before_cleanup_await_and_routes_old_results():
    sender = BlockingRevokeSender()
    owner, coordinator, _, _ = session(sender=sender)
    selected = binding("50000000-0000-0000-0000-000000000005", 1)
    selected_server = server()
    coordinator.snapshot = snapshot(1, selected, selected_server)
    await owner.activate(observation(selected))
    old = owner._current
    submit = asyncio.create_task(owner.runtime_port.submit(action_request("pending")))
    while not sender.sent:
        await asyncio.sleep(0)
    wire_id = sender.sent[0].message_id
    coordinator.snapshot = None
    retiring = asyncio.create_task(owner.synchronize())
    await sender.revoke_entered.wait()
    retiring.cancel()
    await retiring
    assert owner._current is None
    assert owner._retired == [old]
    assert old.current is False
    assert (await submit).admission is MinecraftPortAdmission.UNKNOWN
    with pytest.raises(ControlSessionError):
        await owner.runtime_port.submit(action_request("cannot-revive"))
    trusted_origin = old.context.server_origin
    owner.receive_authoritative(
        authoritative("admission", "b0000000-0000-0000-0000-00000000000b", wire_id, selected),
        origin=trusted_origin,
    )
    owner.receive_authoritative(
        authoritative("receipt", "c0000000-0000-0000-0000-00000000000c", wire_id, selected),
        origin=trusted_origin,
    )
    assert old.ledger.record(wire_id).receipt.payload["status"] == "UNKNOWN"
    await owner.synchronize()
    await owner.close()
    assert owner._retired.count(old) == 1
