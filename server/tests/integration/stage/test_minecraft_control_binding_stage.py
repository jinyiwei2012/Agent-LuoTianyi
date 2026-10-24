import asyncio
from datetime import datetime, timezone

import pytest
from support.world_stage_fakes import ContextFactory, handling_report

from src.adapter.minecraft.control_auth import ControlAuthContext, PendingHandshakes, compute_proof
from src.adapter.minecraft.control_binding import (
    AuthenticatedRoleConnection,
    ControlBindingError,
    ControlBindingPolicy,
    MinecraftControlBindingCoordinator,
    VerifiedBindingActive,
    VerifiedBindingReady,
    VerifiedBodyBound,
)
from src.adapter.minecraft.current_roles import AuthenticatedControlRoleOwner
from src.adapter.minecraft.epoch_store import CommittedEpoch, EpochCandidate
from src.stage import WorldStage


class Agent:
    async def handle_stimulus(self, request, sink, *, context=None):
        return handling_report(request)

    async def realize_action_plan(self, plan, context, sink):
        raise AssertionError


class Store:
    def read_candidate(self):
        return EpochCandidate(PROVISION, BODY, 0, 0, 1)

    def commit_ready(self, candidate, ready):
        return CommittedEpoch(PROVISION, BODY, 1, 1)


class Commands:
    def __init__(self):
        self.revoked = []

    async def send(self, command):
        pass

    async def revoke(self, command):
        self.revoked.append(command)


class BlockingCommitCommands(Commands):
    def __init__(self):
        super().__init__()
        self.commit_entered = asyncio.Event()
        self.release_commit = asyncio.Event()

    async def send(self, command):
        if command.__class__.__name__ == "BindingCommit":
            self.commit_entered.set()
            await self.release_commit.wait()


PROVISION = "10000000-0000-0000-0000-000000000001"
BODY = "20000000-0000-0000-0000-000000000002"
GAME = "30000000-0000-0000-0000-000000000003"
BOOT = "40000000-0000-0000-0000-000000000004"
NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)


def connection(role, install, connection_id):
    context = ControlAuthContext(
        peer_role=role,
        peer_install_id=install,
        hub_install_id="hub",
        key_id="key",
        protocol="luosandbox.minecraft.v1",
        mod_hash=b"m" * 32,
        config_hash=b"c" * 32,
        binding_hash=b"b" * 32,
    )
    challenge = "50000000-0000-4000-8000-000000000005"
    pending = PendingHandshakes()
    item = pending.begin(
        connection_id,
        context,
        b"p" * 32,
        NOW,
        hub_nonce=b"h" * 32,
        challenge_id=challenge,
        now=0,
        utc_now=NOW,
    )
    proof = compute_proof(
        b"k" * 32,
        "PEER_PROOF",
        context,
        item.peer_nonce,
        item.hub_nonce,
        item.challenge_id,
        item.issued_at,
        item.expires_at,
    )
    handshake = pending.consume(
        connection_id,
        challenge,
        proof,
        b"k" * 32,
        expected_context=context,
        now=1,
        utc_now=NOW,
    )
    return AuthenticatedRoleConnection(connection_id=connection_id, handshake=handshake)


def role_owner(server, client):
    owner = AuthenticatedControlRoleOwner((server.handshake.context, client.handshake.context), "hub")
    for connection_value in (server, client):
        reservation = owner.reserve(connection_value.handshake.context, connection_value.connection_id)
        owner.bind_challenge(reservation, connection_value.handshake.challenge_id)
        owner.promote(reservation, connection_value.handshake)
    return owner


@pytest.mark.asyncio
async def test_real_stage_binding_install_and_disconnect_unbinds_old_ingress():
    stage = await WorldStage.create(
        character_id="luotianyi",
        world_id="world",
        agent=Agent(),
        context_factory=ContextFactory(),
    )
    commands = Commands()
    server = connection("SERVER_AUTHORITY", "server-install", "60000000-0000-4000-8000-000000000006")
    client = connection("CLIENT_CONTROLLER", "client-install", "70000000-0000-4000-8000-000000000007")
    roles = role_owner(server, client)
    coordinator = MinecraftControlBindingCoordinator(
        policy=ControlBindingPolicy(
            actor_id="actor",
            world_id="world",
            body_id=BODY,
            character_id="luotianyi",
            provision_id=PROVISION,
            server_install_id="server-install",
            client_install_id="client-install",
        ),
        role_owner=roles,
        epoch_store=Store(),
        commands=commands,
        stage=stage,
        monotonic_now=lambda: 10.0,
        utc_now=lambda: NOW,
    )
    prepare = await coordinator.prepare(
        server,
        VerifiedBodyBound(
            server_boot_id=BOOT,
            game_connection_id=GAME,
            body_id=BODY,
            client_install_id="client-install",
            controller_connection_id=client.connection_id,
            evidence_id="body-proof",
        ),
    )
    await coordinator.ready(server, VerifiedBindingReady(binding=prepare.binding, evidence_id="ready-proof"))
    ingress = await coordinator.activate(
        server, VerifiedBindingActive(binding=prepare.binding, evidence_id="active-proof")
    )
    assert stage.minecraft_observation_scope.session_id == prepare.binding.session_id
    lease = roles.current("CLIENT_CONTROLLER")
    withdrawal = roles.withdraw(lease)
    command = coordinator.fence_role_withdrawal(withdrawal)
    await coordinator.notify_revoke(command)
    assert stage.minecraft_observation_scope is None
    assert coordinator.ingress is None
    assert commands.revoked[-1].binding == prepare.binding
    assert ingress._target is stage
    await stage.close()


@pytest.mark.asyncio
async def test_cancelled_commit_delivery_leaves_real_stage_unbound_and_old_active_rejected():
    stage = await WorldStage.create(
        character_id="luotianyi",
        world_id="world",
        agent=Agent(),
        context_factory=ContextFactory(),
    )
    commands = BlockingCommitCommands()
    server = connection("SERVER_AUTHORITY", "server-install", "60000000-0000-4000-8000-000000000006")
    client = connection("CLIENT_CONTROLLER", "client-install", "70000000-0000-4000-8000-000000000007")
    coordinator = MinecraftControlBindingCoordinator(
        policy=ControlBindingPolicy(
            actor_id="actor",
            world_id="world",
            body_id=BODY,
            character_id="luotianyi",
            provision_id=PROVISION,
            server_install_id="server-install",
            client_install_id="client-install",
        ),
        role_owner=role_owner(server, client),
        epoch_store=Store(),
        commands=commands,
        stage=stage,
        monotonic_now=lambda: 10.0,
        utc_now=lambda: NOW,
    )
    prepare = await coordinator.prepare(
        server,
        VerifiedBodyBound(
            server_boot_id=BOOT,
            game_connection_id=GAME,
            body_id=BODY,
            client_install_id="client-install",
            controller_connection_id=client.connection_id,
            evidence_id="body-proof",
        ),
    )
    task = asyncio.create_task(
        coordinator.ready(server, VerifiedBindingReady(binding=prepare.binding, evidence_id="ready-proof"))
    )
    await commands.commit_entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    try:
        assert stage.minecraft_observation_scope is None
        assert coordinator.ingress is None and coordinator.active is None
        with pytest.raises(ControlBindingError):
            await coordinator.activate(
                server,
                VerifiedBindingActive(binding=prepare.binding, evidence_id="late-active"),
            )
    finally:
        await stage.close()
