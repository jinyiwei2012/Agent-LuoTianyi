import asyncio
from datetime import datetime, timezone

import pytest

from src.adapter.minecraft.control_auth import AuthenticatedHandshake, ControlAuthContext
from src.adapter.minecraft.control_binding import (
    AuthenticatedRoleConnection,
    BindingCommit,
    BindingPrepare,
    ControlBindingError,
    ControlBindingPolicy,
    MinecraftControlBindingCoordinator,
    VerifiedBindingActive,
    VerifiedBindingReady,
    VerifiedBodyBound,
)
from src.adapter.minecraft.epoch_store import CommittedEpoch, EpochCandidate


class Clock:
    def __init__(self):
        self.value = 10.0

    def __call__(self):
        return self.value


class Commands:
    def __init__(self):
        self.sent, self.revoked = [], []
        self.fail_send = False

    async def send(self, command):
        self.sent.append(command)
        if self.fail_send:
            raise RuntimeError("offline send failed")

    async def revoke(self, command):
        self.revoked.append(command)


class BlockingCommands(Commands):
    def __init__(self):
        super().__init__()
        self.send_entered = asyncio.Event()
        self.send_release = asyncio.Event()
        self.revoke_entered = asyncio.Event()
        self.revoke_release = asyncio.Event()
        self.block_revoke = False

    async def send(self, command):
        self.sent.append(command)
        self.send_entered.set()
        await self.send_release.wait()

    async def revoke(self, command):
        self.revoked.append(command)
        self.revoke_entered.set()
        if self.block_revoke:
            await self.revoke_release.wait()


class Store:
    def __init__(self):
        self.last_epoch = 0
        self.generation = 0
        self.commit_calls = 0
        self.fail_commit = False

    def read_candidate(self):
        return EpochCandidate(PROVISION, BODY, self.last_epoch, self.generation, self.last_epoch + 1)

    def commit_ready(self, candidate, ready):
        self.commit_calls += 1
        if self.fail_commit:
            raise RuntimeError("commit failed")
        assert candidate.next_epoch == ready.next_epoch
        self.last_epoch = candidate.next_epoch
        self.generation += 1
        return CommittedEpoch(PROVISION, BODY, self.last_epoch, self.generation)


class Stage:
    character_id = "luotianyi"
    world_id = "world"
    minecraft_observation_scope = None

    def bind_minecraft_observations(self, scope):
        self.minecraft_observation_scope = scope

    def unbind_minecraft_observations(self, scope):
        if self.minecraft_observation_scope != scope:
            return False
        self.minecraft_observation_scope = None
        return True

    async def submit_minecraft_observation(self, fact, scope):
        return scope == self.minecraft_observation_scope


PROVISION = "10000000-0000-0000-0000-000000000001"
BODY = "20000000-0000-0000-0000-000000000002"
GAME = "30000000-0000-0000-0000-000000000003"
BOOT = "40000000-0000-0000-0000-000000000004"


def auth(role, install, connection):
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
    handshake = AuthenticatedHandshake(
        context=context,
        challenge_id="50000000-0000-0000-0000-000000000005",
        ready_proof="proof",
    )
    return AuthenticatedRoleConnection(connection_id=connection, handshake=handshake)


def body_bound(controller="client-conn"):
    return VerifiedBodyBound(
        server_boot_id=BOOT,
        game_connection_id=GAME,
        body_id=BODY,
        client_install_id="client-install",
        controller_connection_id=controller,
        evidence_id="body-proof",
    )


def coordinator(*, store=None, commands=None, stage=None, clock=None):
    return MinecraftControlBindingCoordinator(
        policy=ControlBindingPolicy(
            actor_id="actor",
            world_id="world",
            body_id=BODY,
            character_id="luotianyi",
            provision_id=PROVISION,
            server_install_id="server-install",
            client_install_id="client-install",
        ),
        epoch_store=store,
        commands=commands or Commands(),
        stage=stage or Stage(),
        monotonic_now=clock or Clock(),
        utc_now=lambda: datetime(2026, 10, 23, tzinfo=timezone.utc),
    )


async def registered(*, store=None, commands=None, stage=None, clock=None):
    owner = coordinator(store=store, commands=commands, stage=stage, clock=clock)
    server = auth("SERVER_AUTHORITY", "server-install", "server-conn")
    client = auth("CLIENT_CONTROLLER", "client-install", "client-conn")
    await owner.register_authenticated(server)
    await owner.register_authenticated(client)
    return owner, server, client


@pytest.mark.asyncio
async def test_prepare_ready_commit_active_exact_order_and_idempotence():
    store, commands, stage = Store(), Commands(), Stage()
    owner, server, _ = await registered(store=store, commands=commands, stage=stage)
    prepare = await owner.prepare(server, body_bound())
    assert isinstance(prepare, BindingPrepare)
    assert await owner.prepare(server, body_bound()) == prepare
    assert len(commands.sent) == 1
    with pytest.raises(ControlBindingError):
        await owner.activate(server, VerifiedBindingActive(binding=prepare.binding, evidence_id="too-early"))
    ready = VerifiedBindingReady(binding=prepare.binding, evidence_id="verified-quiescence")
    commit = await owner.ready(server, ready)
    assert isinstance(commit, BindingCommit)
    assert await owner.ready(server, ready) == commit
    assert store.commit_calls == 1
    wrong_active = type(prepare.binding)(
        actor_id=prepare.binding.actor_id,
        world_id=prepare.binding.world_id,
        body_id=prepare.binding.body_id,
        session_id=prepare.binding.session_id,
        session_epoch=prepare.binding.session_epoch,
        server_boot_id="70000000-0000-0000-0000-000000000007",
        game_connection_id=prepare.binding.game_connection_id,
    )
    with pytest.raises(ControlBindingError):
        await owner.activate(server, VerifiedBindingActive(binding=wrong_active, evidence_id="wrong-active"))
    assert stage.minecraft_observation_scope is None
    ingress = await owner.activate(server, VerifiedBindingActive(binding=prepare.binding, evidence_id="active-proof"))
    assert (
        await owner.activate(server, VerifiedBindingActive(binding=prepare.binding, evidence_id="active-proof"))
        is ingress
    )
    assert owner.active.binding == prepare.binding
    assert stage.minecraft_observation_scope.session_id == prepare.binding.session_id


@pytest.mark.asyncio
async def test_wrong_roles_installs_connections_and_body_tuple_fail_closed_before_epoch_write():
    store = Store()
    owner, server, _ = await registered(store=store)
    impostor = auth("CLIENT_CONTROLLER", "client-install", "impostor")
    with pytest.raises(ControlBindingError):
        await owner.prepare(impostor, body_bound())
    with pytest.raises(ControlBindingError):
        await owner.prepare(server, body_bound(controller="wrong-connection"))
    with pytest.raises(ControlBindingError):
        await owner.register_authenticated(auth("SERVER_AUTHORITY", "wrong-install", "wrong"))
    assert store.commit_calls == 0


@pytest.mark.asyncio
async def test_ready_mismatch_timeout_clock_rollback_and_store_failure_never_activate():
    store, commands, stage, clock = Store(), Commands(), Stage(), Clock()
    owner, server, _ = await registered(store=store, commands=commands, stage=stage, clock=clock)
    prepare = await owner.prepare(server, body_bound())
    wrong = type(prepare.binding)(
        actor_id=prepare.binding.actor_id,
        world_id=prepare.binding.world_id,
        body_id=prepare.binding.body_id,
        session_id="60000000-0000-0000-0000-000000000006",
        session_epoch=prepare.binding.session_epoch,
        server_boot_id=prepare.binding.server_boot_id,
        game_connection_id=prepare.binding.game_connection_id,
    )
    with pytest.raises(ControlBindingError):
        await owner.ready(server, VerifiedBindingReady(binding=wrong, evidence_id="wrong"))
    assert store.commit_calls == 0
    clock.value = 15.0
    await owner.check_timeouts()
    assert owner.quarantined and owner.pending_count == 0 and commands.revoked
    owner, server, _ = await registered(store=store, clock=clock)
    await owner.prepare(server, body_bound())
    clock.value = 14.0
    with pytest.raises(ControlBindingError, match="rolled back"):
        await owner.check_timeouts()
    store.fail_commit = True
    owner, server, _ = await registered(store=store)
    prepare = await owner.prepare(server, body_bound())
    with pytest.raises(ControlBindingError, match="epoch commit failed"):
        await owner.ready(server, VerifiedBindingReady(binding=prepare.binding, evidence_id="proof"))
    assert stage.minecraft_observation_scope is None


@pytest.mark.asyncio
async def test_unsupported_store_keeps_roles_registered_but_rejects_activation():
    owner, server, client = await registered(store=None)
    assert server.role == "SERVER_AUTHORITY" and client.role == "CLIENT_CONTROLLER"
    with pytest.raises(ControlBindingError, match="disabled"):
        await owner.prepare(server, body_bound())


@pytest.mark.asyncio
async def test_commit_send_failure_consumes_epoch_and_fences_without_duplicate_write():
    store, commands = Store(), Commands()
    owner, server, _ = await registered(store=store, commands=commands)
    prepare = await owner.prepare(server, body_bound())
    commands.fail_send = True
    with pytest.raises(ControlBindingError, match="uncertain"):
        await owner.ready(server, VerifiedBindingReady(binding=prepare.binding, evidence_id="proof"))
    assert store.last_epoch == 1 and store.commit_calls == 1
    assert owner.active is None and owner.quarantined and commands.revoked
    assert store.read_candidate().next_epoch == 2


@pytest.mark.asyncio
async def test_controller_replacement_and_disconnect_revoke_active_and_old_cannot_revive():
    store, commands, stage = Store(), Commands(), Stage()
    owner, server, client = await registered(store=store, commands=commands, stage=stage)
    prepare = await owner.prepare(server, body_bound())
    await owner.ready(server, VerifiedBindingReady(binding=prepare.binding, evidence_id="ready"))
    await owner.activate(server, VerifiedBindingActive(binding=prepare.binding, evidence_id="active"))
    await owner.register_authenticated(auth("CLIENT_CONTROLLER", "client-install", "client-new"))
    assert owner.active is None and stage.minecraft_observation_scope is None and commands.revoked
    with pytest.raises(ControlBindingError):
        await owner.activate(server, VerifiedBindingActive(binding=prepare.binding, evidence_id="late"))
    await owner.disconnect(client.connection_id)
    assert owner.quarantined


@pytest.mark.asyncio
async def test_prepare_send_cancellation_fences_before_revoke_and_does_not_consume_epoch():
    store, commands, stage = Store(), BlockingCommands(), Stage()
    owner, server, _ = await registered(store=store, commands=commands, stage=stage)
    task = asyncio.create_task(owner.prepare(server, body_bound()))
    await commands.send_entered.wait()
    commands.block_revoke = True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert owner.pending_count == 0
    assert owner.active is None and owner.ingress is None and owner.quarantined
    assert stage.minecraft_observation_scope is None
    assert store.last_epoch == 0 and store.commit_calls == 0
    assert commands.revoked
    with pytest.raises(ControlBindingError):
        await owner.prepare(server, body_bound())


@pytest.mark.asyncio
async def test_commit_send_cancellation_consumes_epoch_then_fences_and_old_active_is_rejected():
    store, commands, stage = Store(), BlockingCommands(), Stage()
    owner, server, _ = await registered(store=store, commands=commands, stage=stage)
    first = asyncio.create_task(owner.prepare(server, body_bound()))
    await commands.send_entered.wait()
    commands.send_release.set()
    prepare = await first
    commands.send_entered.clear()
    commands.send_release.clear()
    task = asyncio.create_task(
        owner.ready(server, VerifiedBindingReady(binding=prepare.binding, evidence_id="verified-ready"))
    )
    await commands.send_entered.wait()
    assert store.last_epoch == 1 and store.commit_calls == 1
    commands.block_revoke = True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert owner.pending_count == 0
    assert owner.active is None and owner.ingress is None and owner.quarantined
    assert stage.minecraft_observation_scope is None
    assert commands.revoked
    assert store.read_candidate().next_epoch == 2
    with pytest.raises(ControlBindingError):
        await owner.activate(
            server,
            VerifiedBindingActive(binding=prepare.binding, evidence_id="late-active"),
        )


@pytest.mark.asyncio
async def test_repeated_cancel_during_revoke_cannot_interrupt_local_fence():
    store, commands, stage = Store(), BlockingCommands(), Stage()
    owner, server, _ = await registered(store=store, commands=commands, stage=stage)
    task = asyncio.create_task(owner.prepare(server, body_bound()))
    await commands.send_entered.wait()
    commands.block_revoke = True
    task.cancel()
    await commands.revoke_entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert owner.pending_count == 0
    assert owner.active is None and owner.ingress is None and owner.quarantined
    assert stage.minecraft_observation_scope is None
