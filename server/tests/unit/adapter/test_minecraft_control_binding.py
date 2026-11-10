import asyncio
import base64
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.adapter.minecraft.control_auth import ControlAuthContext, PendingHandshakes, compute_proof
from src.adapter.minecraft.control_binding import (
    AuthenticatedRoleConnection,
    BindingCommit,
    BindingPhase,
    BindingPrepare,
    ControlBindingError,
    ControlBindingPolicy,
    MinecraftControlBindingCoordinator,
    VerifiedBindingActive,
    VerifiedBindingReady,
    VerifiedBodyBound,
)
from src.adapter.minecraft.current_roles import AuthenticatedControlRoleOwner, RoleWithdrawal
from src.adapter.minecraft.epoch_store import CommittedEpoch, EpochCandidate, PlatformCapability
from src.adapter.minecraft.epoch_worker import EpochPreflightRequest, EpochWorkerConfig, MinecraftEpochStoreWorker
from src.adapter.minecraft.v2_body_routes import V2BodyRouteOwner
from tests.unit.adapter.test_minecraft_v2_body_routes import candidate, notice


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


class BlockingWorker:
    """Delegate to a real worker but hold the Nth wait open for interleaving."""

    def __init__(self, worker, *, block_from=2):
        self.worker = worker
        self.waits = 0
        self.block_from = block_from
        self.wait_entered = asyncio.Event()
        self.wait_release = asyncio.Event()

    @property
    def state(self):
        return self.worker.state

    @property
    def outstanding_count(self):
        return self.worker.outstanding_count

    def submit_preflight(self, request):
        return self.worker.submit_preflight(request)

    def take_preflight(self, handle):
        return self.worker.take_preflight(handle)

    def validates_preflight(self, snapshot, **kwargs):
        return self.worker.validates_preflight(snapshot, **kwargs)

    def submit_commit(self, request):
        return self.worker.submit_commit(request)

    def take_commit(self, handle):
        return self.worker.take_commit(handle)

    def mark_obsolete(self, value):
        return self.worker.mark_obsolete(value)

    async def wait(self, handle):
        self.waits += 1
        if self.waits >= self.block_from:
            self.wait_entered.set()
            await self.wait_release.wait()
        await self.worker.wait(handle)

    async def close(self):
        await self.worker.close()


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


class ReentrantStore(Store):
    def __init__(self):
        super().__init__()
        self.on_read = None
        self.on_commit = None
        self.fail_read = False
        self.read_result = None

    def read_candidate(self):
        candidate = self.read_result or super().read_candidate()
        if self.on_read is not None:
            self.on_read()
        if self.fail_read:
            raise RuntimeError("read failed")
        return candidate

    def commit_ready(self, candidate, ready):
        committed = super().commit_ready(candidate, ready)
        if self.on_commit is not None:
            self.on_commit()
        return committed


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


class RaisingUnbindStage(Stage):
    def __init__(self, *, clear_first):
        self.minecraft_observation_scope = None
        self.clear_first = clear_first
        self.observed = None
        self.coordinator = None

    def unbind_minecraft_observations(self, scope):
        self.observed = (
            self.coordinator.active,
            self.coordinator.ingress,
            self.coordinator.pending_count,
            self.coordinator.quarantined,
            self.coordinator.active_snapshot(),
            self.coordinator._generation,
        )
        if self.clear_first:
            self.minecraft_observation_scope = None
        raise RuntimeError("unbind failed")


PROVISION = "10000000-0000-0000-0000-000000000001"
BODY = "20000000-0000-0000-0000-000000000002"
GAME = "30000000-0000-0000-0000-000000000003"
BOOT = "40000000-0000-0000-0000-000000000004"
SERVER_CONNECTION = "60000000-0000-4000-8000-000000000006"
CLIENT_CONNECTION = "70000000-0000-4000-8000-000000000007"


def auth(
    role,
    install,
    connection,
    *,
    challenge="50000000-0000-4000-8000-000000000005",
    selected_context=None,
):
    context = selected_context or ControlAuthContext(
        peer_role=role,
        peer_install_id=install,
        hub_install_id="hub",
        key_id="key",
        protocol="luosandbox.minecraft.v1",
        mod_hash=b"m" * 32,
        config_hash=b"c" * 32,
        binding_hash=b"b" * 32,
    )
    now = datetime(2026, 10, 23, tzinfo=timezone.utc)
    pending = PendingHandshakes()
    item = pending.begin(
        connection,
        context,
        b"p" * 32,
        now,
        hub_nonce=b"h" * 32,
        challenge_id=challenge,
        now=0,
        utc_now=now,
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
        connection,
        challenge,
        proof,
        b"k" * 32,
        expected_context=context,
        now=1,
        utc_now=now,
    )
    return AuthenticatedRoleConnection(connection_id=connection, handshake=handshake)


def body_bound(controller=CLIENT_CONNECTION):
    return VerifiedBodyBound(
        server_boot_id=BOOT,
        game_connection_id=GAME,
        body_id=BODY,
        client_install_id="client-install",
        controller_connection_id=controller,
        evidence_id="body-proof",
    )


def coordinator(*, role_owner, store=None, worker=None, commands=None, stage=None, clock=None):
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
        role_owner=role_owner,
        epoch_store=store,
        epoch_worker=worker,
        commands=commands or Commands(),
        stage=stage or Stage(),
        monotonic_now=clock or Clock(),
        utc_now=lambda: datetime(2026, 10, 23, tzinfo=timezone.utc),
    )


async def registered(*, store=None, commands=None, stage=None, clock=None):
    server = auth("SERVER_AUTHORITY", "server-install", SERVER_CONNECTION)
    client = auth("CLIENT_CONTROLLER", "client-install", CLIENT_CONNECTION)
    role_owner = AuthenticatedControlRoleOwner((server.handshake.context, client.handshake.context), "hub")
    for connection in (server, client):
        reservation = role_owner.reserve(connection.handshake.context, connection.connection_id)
        role_owner.bind_challenge(reservation, connection.handshake.challenge_id)
        role_owner.promote(reservation, connection.handshake)
    binding = coordinator(role_owner=role_owner, store=store, commands=commands, stage=stage, clock=clock)
    return binding, server, client


class WorkerPlatform:
    def capability(self, directory):
        return PlatformCapability(True, "test-only")

    def acquire_lock(self, path):
        return type("Lock", (), {"close": lambda self: None})()


async def v2_fixture(tmp_path, commands=None):
    server = auth("SERVER_AUTHORITY", "server-install", SERVER_CONNECTION)
    client = auth("CLIENT_CONTROLLER", "client-install", CLIENT_CONNECTION)
    roles = AuthenticatedControlRoleOwner((server.handshake.context, client.handshake.context), "hub")
    leases = {}
    for connection in (server, client):
        reservation = roles.reserve(connection.handshake.context, connection.connection_id)
        roles.bind_challenge(reservation, connection.handshake.challenge_id)
        leases[connection.role] = roles.promote(reservation, connection.handshake)
    worker = MinecraftEpochStoreWorker(
        EpochWorkerConfig(
            mode="PROVISION",
            directory=(tmp_path / "epoch").resolve(),
            platform=WorkerPlatform(),
            provision_id=PROVISION,
            body_id=BODY,
        )
    )
    await worker.start()
    binding = coordinator(role_owner=roles, worker=worker, commands=commands or Commands())
    routes = V2BodyRouteOwner(
        role_owner=roles,
        monotonic_millis=lambda: 0,
        utc_now=lambda: datetime(2026, 10, 24, tzinfo=timezone.utc),
        route_id=lambda: "80000000-0000-4000-8000-000000000008",
    )
    notice_payload = json.dumps(
        {
            "security": "luosandbox.control-auth.v1",
            "kind": "BODY_BIND_NOTICE",
            "serverBootId": BOOT,
            "gameConnectionId": GAME,
            "bodyId": BODY,
            "gameNonce": base64.urlsafe_b64encode(b"n" * 32).decode().rstrip("="),
        },
        separators=(",", ":"),
    ).encode()
    notice_frame = replace(notice(), connection_id=CLIENT_CONNECTION, payload=notice_payload)
    routes.route_notice(leases["CLIENT_CONTROLLER"], notice_frame, lambda *args: None)
    candidate_payload = json.dumps(
        {
            "security": "luosandbox.control-auth.v1",
            "kind": "BODY_BOUND",
            "serverBootId": BOOT,
            "gameConnectionId": GAME,
            "bodyId": BODY,
        },
        separators=(",", ":"),
    ).encode()
    accepted = routes.accept_candidate(
        leases["SERVER_AUTHORITY"],
        replace(
            candidate("80000000-0000-4000-8000-000000000008", body=BODY),
            connection_id=SERVER_CONNECTION,
            recipient_connection_id=SERVER_CONNECTION,
            payload=candidate_payload,
        ),
    )
    return binding, routes, accepted, server, client, worker


@pytest.mark.asyncio
async def test_v2_headless_admission_early_ready_commits_after_send_without_activation(tmp_path):
    commands = BlockingCommands()
    binding, routes, accepted, server, _, worker = await v2_fixture(tmp_path, commands)
    dispatch = await binding.prepare_v2(routes, accepted.receipt)
    prepare = dispatch.command
    assert binding.pending_count == 1 and worker.outstanding_count == 1
    with pytest.raises(ControlBindingError, match="headless"):
        await binding.ready(server, VerifiedBindingReady(binding=prepare.binding, evidence_id="legacy"))
    task = asyncio.create_task(binding.dispatch_v2_prepare(dispatch))
    await commands.send_entered.wait()
    binding.receive_v2_ready(server, VerifiedBindingReady(binding=prepare.binding, evidence_id="early"))
    ticket = binding._v2_tickets[prepare.binding.session_id]
    pending = binding._pending[prepare.binding.session_id]
    assert ticket.ready is not None and ticket.state.value == "DISPATCHING"
    assert pending.phase is BindingPhase.PREPARE
    commands.send_release.set()
    assert await task == prepare
    with pytest.raises(ControlBindingError):
        await binding.dispatch_v2_prepare(dispatch)
    assert ticket.state.value == "DELIVERED_CURRENT"
    assert pending.phase is BindingPhase.COMMIT and pending.commit_io_pending is False
    assert pending.committed is not None
    assert pending.committed.last_epoch == prepare.binding.session_epoch
    assert [type(command) for command in commands.sent] == [BindingPrepare, BindingCommit]
    assert ticket.commit is not None
    assert binding.active is None and binding.ingress is None
    await worker.close()


@pytest.mark.asyncio
async def test_v2_ready_after_dispatch_commits_and_requires_explicit_active(tmp_path):
    commands = Commands()
    binding, routes, accepted, server, _, worker = await v2_fixture(tmp_path, commands)
    dispatch = await binding.prepare_v2(routes, accepted.receipt)
    prepare = dispatch.command
    assert await binding.dispatch_v2_prepare(dispatch) == prepare
    pending = binding._pending[prepare.binding.session_id]
    assert pending.phase is BindingPhase.PREPARE and worker.outstanding_count == 1
    commit = await binding.admit_v2_ready(server, VerifiedBindingReady(binding=prepare.binding, evidence_id="ready"))
    assert commit is not None and commit.binding == prepare.binding
    assert [type(command) for command in commands.sent] == [BindingPrepare, BindingCommit]
    assert pending.phase is BindingPhase.COMMIT and pending.commit_io_pending is False
    assert worker.outstanding_count == 0
    assert binding.active is None and binding.ingress is None
    active = VerifiedBindingActive(binding=prepare.binding, evidence_id="active")
    ingress = await binding.activate(server, active)
    assert ingress is not None and binding.active is not None
    assert await binding.activate(server, active) is ingress
    assert binding.pending_count == 0
    await worker.close()


@pytest.mark.asyncio
async def test_v2_ready_settlement_rechecks_obsolete_preflight(tmp_path):
    binding, routes, accepted, server, _, worker = await v2_fixture(tmp_path)
    dispatch = await binding.prepare_v2(routes, accepted.receipt)
    prepare = dispatch.command
    await binding.dispatch_v2_prepare(dispatch)
    pending = binding._pending[prepare.binding.session_id]
    worker.mark_obsolete(pending.preflight)
    with pytest.raises(ControlBindingError, match="preflight is stale"):
        await binding.admit_v2_ready(server, VerifiedBindingReady(binding=prepare.binding, evidence_id="ready"))
    assert binding.quarantined and binding.pending_count == 0
    await worker.close()


@pytest.mark.asyncio
async def test_v2_commit_send_failure_fences_after_durable_cas(tmp_path):
    commands = Commands()
    binding, routes, accepted, server, _, worker = await v2_fixture(tmp_path, commands)
    dispatch = await binding.prepare_v2(routes, accepted.receipt)
    prepare = dispatch.command
    await binding.dispatch_v2_prepare(dispatch)
    commands.fail_send = True
    with pytest.raises(ControlBindingError, match="uncertain"):
        await binding.admit_v2_ready(server, VerifiedBindingReady(binding=prepare.binding, evidence_id="ready"))
    assert binding.quarantined and binding.pending_count == 0
    assert binding.active is None
    await worker.close()


@pytest.mark.asyncio
async def test_v2_disconnect_during_commit_io_pending_fences_and_leaves_epoch_gap(tmp_path):
    commands = Commands()
    binding, routes, accepted, server, _, worker = await v2_fixture(tmp_path, commands)
    blocking = BlockingWorker(worker)
    binding._epoch_worker = blocking
    dispatch = await binding.prepare_v2(routes, accepted.receipt)
    prepare = dispatch.command
    await binding.dispatch_v2_prepare(dispatch)
    pending = binding._pending[prepare.binding.session_id]
    task = asyncio.create_task(
        binding.admit_v2_ready(server, VerifiedBindingReady(binding=prepare.binding, evidence_id="ready"))
    )
    await blocking.wait_entered.wait()
    assert pending.commit_io_pending is True
    assert worker.outstanding_count == 1
    lease = binding.role_owner.current("CLIENT_CONTROLLER")
    withdrawal = binding.role_owner.withdraw(lease)
    revoke = binding.fence_role_withdrawal(withdrawal)
    assert binding.quarantined and binding.pending_count == 0
    blocking.wait_release.set()
    with pytest.raises(ControlBindingError):
        await task
    await binding.notify_revoke(revoke)
    assert [type(item) for item in commands.sent] == [BindingPrepare]
    assert binding.active is None and binding.ingress is None
    assert binding._epoch_worker is blocking
    handle = worker.submit_preflight(
        EpochPreflightRequest(
            provision_id=PROVISION,
            body_id=BODY,
            coordinator_generation=0,
            route_id="b0000000-0000-4000-8000-00000000000b",
            server_connection_id=SERVER_CONNECTION,
            controller_connection_id=CLIENT_CONNECTION,
        )
    )
    await worker.wait(handle)
    snapshot = worker.take_preflight(handle)
    assert snapshot.expected_last_epoch == prepare.binding.session_epoch
    assert snapshot.next_epoch == prepare.binding.session_epoch + 1
    await worker.close()


@pytest.mark.asyncio
async def test_v2_duplicate_ready_after_commit_fences_without_second_commit(tmp_path):
    commands = Commands()
    binding, routes, accepted, server, _, worker = await v2_fixture(tmp_path, commands)
    dispatch = await binding.prepare_v2(routes, accepted.receipt)
    prepare = dispatch.command
    await binding.dispatch_v2_prepare(dispatch)
    await binding.admit_v2_ready(server, VerifiedBindingReady(binding=prepare.binding, evidence_id="ready"))
    assert [type(item) for item in commands.sent] == [BindingPrepare, BindingCommit]
    with pytest.raises(ControlBindingError, match="duplicate"):
        await binding.admit_v2_ready(server, VerifiedBindingReady(binding=prepare.binding, evidence_id="again"))
    assert binding.quarantined and binding.pending_count == 0
    assert [type(item) for item in commands.sent] == [BindingPrepare, BindingCommit]
    assert binding.active is None
    await worker.close()


@pytest.mark.asyncio
async def test_v2_activate_before_commit_is_rejected(tmp_path):
    binding, routes, accepted, server, _, worker = await v2_fixture(tmp_path)
    dispatch = await binding.prepare_v2(routes, accepted.receipt)
    prepare = dispatch.command
    await binding.dispatch_v2_prepare(dispatch)
    with pytest.raises(ControlBindingError, match="not current"):
        await binding.activate(server, VerifiedBindingActive(binding=prepare.binding, evidence_id="active"))
    assert binding.active is None and binding.pending_count == 1
    binding._fence_local("QUARANTINE")
    await worker.close()


@pytest.mark.asyncio
async def test_v2_default_worker_unavailable_and_duplicate_ready_fence(tmp_path):
    owner, _, _ = await registered()
    assert not owner.v2_admission_available()
    commands = BlockingCommands()
    binding, routes, accepted, server, _, worker = await v2_fixture(tmp_path, commands)
    dispatch = await binding.prepare_v2(routes, accepted.receipt)
    prepare = dispatch.command
    task = asyncio.create_task(binding.dispatch_v2_prepare(dispatch))
    await commands.send_entered.wait()
    ready = VerifiedBindingReady(binding=prepare.binding, evidence_id="one")
    binding.receive_v2_ready(server, ready)
    with pytest.raises(ControlBindingError, match="duplicate"):
        binding.receive_v2_ready(server, ready)
    assert binding.pending_count == 0 and binding.quarantined
    commands.send_release.set()
    await asyncio.gather(task, return_exceptions=True)
    await worker.close()


@pytest.mark.asyncio
async def test_v2_dispatch_capability_copy_and_equal_binding_are_not_authority(tmp_path):
    import copy

    binding, routes, accepted, _, _, worker = await v2_fixture(tmp_path)
    dispatch = await binding.prepare_v2(routes, accepted.receipt)
    with pytest.raises(ControlBindingError):
        await binding.dispatch_v2_prepare(copy.copy(dispatch))
    with pytest.raises(AttributeError):
        dispatch.command = BindingPrepare(binding=replace(dispatch.command.binding))
    equal_binding = replace(dispatch.command.binding)
    with pytest.raises(ControlBindingError):
        await binding.dispatch_v2_prepare(equal_binding)
    assert binding.pending_count == 1 and worker.outstanding_count == 1
    binding._fence_local("QUARANTINE")
    await worker.close()


@pytest.mark.asyncio
async def test_conflicting_ready_fences_current_ticket(tmp_path):
    commands = BlockingCommands()
    binding, routes, accepted, server, _, worker = await v2_fixture(tmp_path, commands)
    dispatch = await binding.prepare_v2(routes, accepted.receipt)
    task = asyncio.create_task(binding.dispatch_v2_prepare(dispatch))
    await commands.send_entered.wait()
    wrong = replace(dispatch.command.binding, body_id="f0000000-0000-4000-8000-00000000000f")
    with pytest.raises(ControlBindingError, match="conflicting"):
        binding.receive_v2_ready(server, VerifiedBindingReady(binding=wrong, evidence_id="wrong"))
    assert binding.pending_count == 0 and binding.quarantined
    commands.send_release.set()
    await asyncio.gather(task, return_exceptions=True)
    await worker.close()


@pytest.mark.asyncio
async def test_v2_send_return_after_role_loss_is_never_success(tmp_path):
    commands = BlockingCommands()
    binding, routes, accepted, _, _, worker = await v2_fixture(tmp_path, commands)
    dispatch = await binding.prepare_v2(routes, accepted.receipt)
    task = asyncio.create_task(binding.dispatch_v2_prepare(dispatch))
    await commands.send_entered.wait()
    lease = binding.role_owner.current("CLIENT_CONTROLLER")
    withdrawal = binding.role_owner.withdraw(lease)
    command = binding.fence_role_withdrawal(withdrawal)
    commands.send_release.set()
    with pytest.raises(ControlBindingError):
        await task
    await binding.notify_revoke(command)
    assert binding.pending_count == 0 and binding.quarantined
    await worker.close()


@pytest.mark.asyncio
async def test_prepare_v2_submission_failure_invalidates_claimed_receipt(tmp_path):
    binding, routes, accepted, _, _, worker = await v2_fixture(tmp_path)
    first = worker.submit_preflight(
        EpochPreflightRequest(
            provision_id=PROVISION,
            body_id=BODY,
            coordinator_generation=99,
            route_id="90000000-0000-4000-8000-000000000009",
            server_connection_id=SERVER_CONNECTION,
            controller_connection_id=CLIENT_CONNECTION,
        )
    )
    second = worker.submit_preflight(
        EpochPreflightRequest(
            provision_id=PROVISION,
            body_id=BODY,
            coordinator_generation=100,
            route_id="a0000000-0000-4000-8000-00000000000a",
            server_connection_id=SERVER_CONNECTION,
            controller_connection_id=CLIENT_CONNECTION,
        )
    )
    with pytest.raises(ControlBindingError):
        await binding.prepare_v2(routes, accepted.receipt)
    record = routes._routes[accepted.candidate.route_id]
    assert not record.preflight_claimed and record.receipt is None
    assert record.deadline == 5000 and binding.pending_count == 0
    worker.mark_obsolete(first)
    worker.mark_obsolete(second)
    await worker.close()


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
        await owner.prepare(server, body_bound(controller="80000000-0000-4000-8000-000000000008"))
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
async def test_controller_withdrawal_revokes_active_and_old_cannot_revive():
    store, commands, stage = Store(), Commands(), Stage()
    owner, server, client = await registered(store=store, commands=commands, stage=stage)
    prepare = await owner.prepare(server, body_bound())
    await owner.ready(server, VerifiedBindingReady(binding=prepare.binding, evidence_id="ready"))
    await owner.activate(server, VerifiedBindingActive(binding=prepare.binding, evidence_id="active"))
    role_owner = owner.role_owner
    lease = role_owner.current("CLIENT_CONTROLLER")
    withdrawal = role_owner.withdraw(lease)
    command = owner.fence_role_withdrawal(withdrawal)
    assert owner.active is None and owner.ingress is None and owner.pending_count == 0
    assert stage.minecraft_observation_scope is None and owner.quarantined
    assert not commands.revoked
    await owner.notify_revoke(command)
    assert owner.active is None and stage.minecraft_observation_scope is None and commands.revoked
    with pytest.raises(ControlBindingError):
        await owner.activate(server, VerifiedBindingActive(binding=prepare.binding, evidence_id="late"))
    assert role_owner.withdraw(lease) is None
    assert owner.quarantined


@pytest.mark.asyncio
async def test_stale_unrelated_withdrawal_cannot_fence_replacement_generation():
    store, commands, stage = Store(), Commands(), Stage()
    owner, server, _ = await registered(store=store, commands=commands, stage=stage)
    prepare = await owner.prepare(server, body_bound())
    await owner.ready(server, VerifiedBindingReady(binding=prepare.binding, evidence_id="ready"))
    await owner.activate(server, VerifiedBindingActive(binding=prepare.binding, evidence_id="active"))
    stale = RoleWithdrawal("CLIENT_CONTROLLER", "80000000-0000-4000-8000-000000000008", 1)
    assert owner.fence_role_withdrawal(stale) is None
    assert owner.active is not None and stage.minecraft_observation_scope is not None


@pytest.mark.asyncio
async def test_forged_same_payload_and_copied_withdrawal_cannot_fence():
    import copy

    store, commands, stage = Store(), Commands(), Stage()
    owner, server, _ = await registered(store=store, commands=commands, stage=stage)
    prepare = await owner.prepare(server, body_bound())
    await owner.ready(server, VerifiedBindingReady(binding=prepare.binding, evidence_id="ready"))
    await owner.activate(server, VerifiedBindingActive(binding=prepare.binding, evidence_id="active"))
    lease = owner.role_owner.current("CLIENT_CONTROLLER")
    forged = RoleWithdrawal(lease.role, lease.connection_id, lease.generation)
    assert owner.fence_role_withdrawal(forged) is None
    withdrawal = owner.role_owner.withdraw(lease)
    copied = copy.copy(withdrawal)
    assert owner.fence_role_withdrawal(copied) is None
    command = owner.fence_role_withdrawal(withdrawal)
    assert command is not None and owner.active is None
    assert owner.fence_role_withdrawal(withdrawal) is None


@pytest.mark.asyncio
async def test_prepare_send_withdrawal_cannot_return_stale_success():
    store, commands, stage = Store(), BlockingCommands(), Stage()
    owner, server, _ = await registered(store=store, commands=commands, stage=stage)
    task = asyncio.create_task(owner.prepare(server, body_bound()))
    await commands.send_entered.wait()
    lease = owner.role_owner.current("CLIENT_CONTROLLER")
    withdrawal = owner.role_owner.withdraw(lease)
    command = owner.fence_role_withdrawal(withdrawal)
    commands.send_release.set()
    with pytest.raises(ControlBindingError, match="authority changed"):
        await task
    assert owner.pending_count == 0 and owner.active is None and owner.quarantined
    await owner.notify_revoke(command)


@pytest.mark.asyncio
async def test_reentrant_candidate_read_withdrawal_never_publishes_pending():
    store = ReentrantStore()
    owner, server, _ = await registered(store=store)
    command = None

    def withdraw():
        nonlocal command
        lease = owner.role_owner.current("CLIENT_CONTROLLER")
        command = owner.fence_role_withdrawal(owner.role_owner.withdraw(lease))

    store.on_read = withdraw
    with pytest.raises(ControlBindingError, match="candidate read"):
        await owner.prepare(server, body_bound())
    assert owner.pending_count == 0 and owner.active is None and not owner.quarantined
    await owner.notify_revoke(command)


@pytest.mark.asyncio
@pytest.mark.parametrize("read_error", (False, True))
async def test_stale_candidate_read_cannot_clear_same_connection_new_generation(read_error):
    store = ReentrantStore()
    owner, server, client = await registered(store=store)
    existing = await owner.prepare(server, body_bound())
    old_pending = owner._pending[existing.binding.session_id]
    old_client = owner.role_owner.current("CLIENT_CONTROLLER")
    replacement_challenge = "90000000-0000-4000-8000-000000000009"

    def replace_generation():
        owner.role_owner.withdraw(old_client)
        reservation = owner.role_owner.reserve(client.handshake.context, client.connection_id)
        owner.role_owner.bind_challenge(reservation, replacement_challenge)
        replacement = auth(
            "CLIENT_CONTROLLER",
            "client-install",
            client.connection_id,
            challenge=replacement_challenge,
            selected_context=client.handshake.context,
        )
        new_client = owner.role_owner.promote(reservation, replacement.handshake)
        replacement_pending = replace(old_pending, controller_lease=new_client)
        owner._pending = {replacement_pending.binding.session_id: replacement_pending}
        owner._generation += 1

    store.on_read = replace_generation
    store.fail_read = read_error
    changed = body_bound()
    object.__setattr__(changed, "evidence_id", "different-evidence")
    with pytest.raises(ControlBindingError):
        await owner.prepare(server, changed)
    assert owner._pending[existing.binding.session_id] is not old_pending
    assert owner.pending_count == 1 and not owner.quarantined


@pytest.mark.asyncio
@pytest.mark.parametrize("read_error", (False, True))
@pytest.mark.parametrize("mutation", ("generation", "pending"))
async def test_candidate_read_state_change_with_same_current_leases_preserves_replacement(read_error, mutation):
    store, commands = ReentrantStore(), Commands()
    owner, server, _ = await registered(store=store, commands=commands)
    existing = await owner.prepare(server, body_bound())
    captured_pending = owner._pending[existing.binding.session_id]
    server_lease = owner.role_owner.current("SERVER_AUTHORITY")
    controller_lease = owner.role_owner.current("CLIENT_CONTROLLER")
    replacement = None

    def change_state():
        nonlocal replacement
        assert owner.role_owner.current("SERVER_AUTHORITY") is server_lease
        assert owner.role_owner.current("CLIENT_CONTROLLER") is controller_lease
        if mutation == "generation":
            owner._generation += 1
        else:
            replacement = replace(captured_pending)
            owner._pending = {replacement.binding.session_id: replacement}

    store.on_read = change_state
    store.fail_read = read_error
    if not read_error:
        store.read_result = EpochCandidate(
            "90000000-0000-4000-8000-000000000009",
            BODY,
            0,
            0,
            1,
        )
    changed = body_bound()
    object.__setattr__(changed, "evidence_id", "different-evidence")
    with pytest.raises(ControlBindingError):
        await owner.prepare(server, changed)
    assert owner.role_owner.current("SERVER_AUTHORITY") is server_lease
    assert owner.role_owner.current("CLIENT_CONTROLLER") is controller_lease
    if mutation == "generation":
        assert owner._pending[existing.binding.session_id] is captured_pending
        assert owner._generation == 1
    else:
        assert owner._pending[existing.binding.session_id] is replacement
        assert owner._generation == 0
    assert owner.pending_count == 1 and not owner.quarantined
    assert len(commands.sent) == 1 and store.last_epoch == 0


@pytest.mark.asyncio
async def test_reentrant_commit_withdrawal_consumes_epoch_but_never_sends_commit():
    store, commands = ReentrantStore(), Commands()
    owner, server, _ = await registered(store=store, commands=commands)
    prepare = await owner.prepare(server, body_bound())
    command = None

    def withdraw():
        nonlocal command
        lease = owner.role_owner.current("CLIENT_CONTROLLER")
        command = owner.fence_role_withdrawal(owner.role_owner.withdraw(lease))

    store.on_commit = withdraw
    with pytest.raises(ControlBindingError, match="epoch commit completed"):
        await owner.ready(server, VerifiedBindingReady(binding=prepare.binding, evidence_id="ready"))
    assert store.last_epoch == 1 and len(commands.sent) == 1
    assert owner.pending_count == 0 and owner.active is None and owner.quarantined
    await owner.notify_revoke(command)


@pytest.mark.asyncio
@pytest.mark.parametrize("clear_first", (False, True))
async def test_active_unbind_failure_observes_local_authority_already_cleared(clear_first):
    store, commands = Store(), Commands()
    stage = RaisingUnbindStage(clear_first=clear_first)
    owner, server, _ = await registered(store=store, commands=commands, stage=stage)
    stage.coordinator = owner
    prepare = await owner.prepare(server, body_bound())
    await owner.ready(server, VerifiedBindingReady(binding=prepare.binding, evidence_id="ready"))
    await owner.activate(server, VerifiedBindingActive(binding=prepare.binding, evidence_id="active"))
    generation = owner._generation
    lease = owner.role_owner.current("CLIENT_CONTROLLER")
    withdrawal = owner.role_owner.withdraw(lease)
    with pytest.raises(RuntimeError, match="unbind failed"):
        owner.fence_role_withdrawal(withdrawal)
    assert stage.observed == (None, None, 0, True, None, generation + 1)
    assert owner.active is None and owner.ingress is None and owner.pending_count == 0
    assert owner.quarantined and owner.active_snapshot() is None
    if clear_first:
        assert stage.minecraft_observation_scope is None
    else:
        assert stage.minecraft_observation_scope is not None


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
