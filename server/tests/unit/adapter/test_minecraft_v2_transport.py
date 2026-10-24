import asyncio
import base64
import ctypes
import json
import os
import struct
import threading
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.adapter.minecraft.control_auth import ControlAuthError, PendingHandshakes, b32_encode, compute_proof
from src.adapter.minecraft.control_binding import (
    AuthenticatedRoleConnection,
    BindingCommit,
    BindingPhase,
    BindingPrepare,
    ControlBindingPolicy,
    MinecraftControlBindingCoordinator,
    VerifiedBodyBound,
)
from src.adapter.minecraft.current_roles import AuthenticatedControlRoleOwner
from src.adapter.minecraft.deployment_acl import TrustedDeploymentAclPolicy, inspect_handle_security_descriptor
from src.adapter.minecraft.deployment_files import (
    DeploymentFileProvision,
    TrustedDeploymentRoot,
    _file_info,
    load_verified_deployment_filesystem,
)
from src.adapter.minecraft.deployment_manifest import DeploymentManifest, TrustedDeploymentIdentity
from src.adapter.minecraft.epoch_store import EpochCandidate, EpochStore, PlatformCapability
from src.adapter.minecraft.epoch_worker import (
    EpochPreflightRequest,
    EpochWorkerConfig,
    MinecraftEpochStoreWorker,
)
from src.adapter.minecraft.postauth_frame import PostAuthFrame, TrustedFrameContext, encode_frame
from src.adapter.minecraft.postauth_keys import AuthenticatedTranscript, PostAuthSessionKeys
from src.adapter.minecraft.postauth_session import PostAuthSessionState
from src.adapter.minecraft.v2_startup import provision_v2_peer
from src.adapter.minecraft.v2_transport import (
    _MAX_SAFE_AUTH_MILLIS,
    MinecraftV2AuthenticatedTransport,
    V2TransportError,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="v2 transport requires native verified NTFS evidence")

MOD = b"public-mod-manifest"
POLICY = b"public-policy-config"
BINDING = b"public-binding-manifest"
PSK = bytes(range(32))
NOW = datetime(2026, 1, 2, 3, 4, 5, 678000, tzinfo=timezone.utc)
CHALLENGE_ID = "11111111-2222-4333-8444-555555555555"
INTEGRATED_PROVISION = "10000000-0000-4000-8000-000000000001"
INTEGRATED_BODY = "20000000-0000-4000-8000-000000000002"
PEER_NONCE = b"p" * 32
HUB_NONCE = b"h" * 32


class Clock:
    def __init__(self):
        self.value = 10_000

    def __call__(self):
        return self.value


class BindingStage:
    character_id = "luotianyi"
    world_id = "world"
    minecraft_observation_scope = None

    def bind_minecraft_observations(self, scope):
        self.minecraft_observation_scope = scope

    def unbind_minecraft_observations(self, scope):
        self.minecraft_observation_scope = None
        return True


class BindingCommands:
    def __init__(self):
        self.sent = []
        self.revoke_entered = asyncio.Event()
        self.revoke_release = asyncio.Event()
        self.block_revoke = False

    async def send(self, command):
        self.sent.append(command)
        return None

    async def revoke(self, command):
        self.revoke_entered.set()
        if self.block_revoke:
            await self.revoke_release.wait()


class BlockingPrepareCommands(BindingCommands):
    def __init__(self, *, hold_cancel=False):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = 0
        self.hold_cancel = hold_cancel

    async def send(self, command):
        self.sent.append(command)
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            if self.hold_cancel:
                await self.release.wait()
            raise


class CapacityPrepareCommands(BindingCommands):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def send(self, command):
        self.sent.append(command)
        self.entered.set()
        await self.release.wait()


class BindingStore:
    def read_candidate(self):
        return EpochCandidate(INTEGRATED_PROVISION, INTEGRATED_BODY, 0, 0, 1)


class IntegratedStage(BindingStage):
    def __init__(self):
        self.minecraft_observation_scope = None


def _coordinator(owner, *, commands=None, stage=None, store=None, worker=None):
    return MinecraftControlBindingCoordinator(
        policy=ControlBindingPolicy(
            actor_id="actor",
            world_id="world",
            body_id=INTEGRATED_BODY,
            character_id="luotianyi",
            provision_id=INTEGRATED_PROVISION,
            server_install_id="server-test-1",
            client_install_id="client-test-1",
        ),
        role_owner=owner,
        epoch_store=store,
        epoch_worker=worker,
        commands=commands or BindingCommands(),
        stage=stage or BindingStage(),
        monotonic_now=lambda: 10.0,
        utc_now=lambda: NOW,
    )


class AdmissionPlatform:
    def capability(self, directory):
        return PlatformCapability(True, "test-only")

    def acquire_lock(self, path):
        return type("Lock", (), {"close": lambda self: None})()


@pytest.mark.asyncio
async def test_transport_candidate_stays_prepare_only_and_reopen_preserves_initial_epoch(peers, tmp_path):
    clock = Clock()
    route_body = "11111111-2222-4333-8444-555555555553"
    epoch_directory = (tmp_path / "epoch").resolve()
    roles = AuthenticatedControlRoleOwner(tuple(peer.context for peer in peers), "hub-test-1")
    worker = MinecraftEpochStoreWorker(
        EpochWorkerConfig(
            mode="PROVISION",
            directory=epoch_directory,
            platform=AdmissionPlatform(),
            provision_id=INTEGRATED_PROVISION,
            body_id=route_body,
        )
    )
    await worker.start()
    commands = BindingCommands()
    stage = IntegratedStage()
    coordinator = MinecraftControlBindingCoordinator(
        policy=ControlBindingPolicy(
            actor_id="actor",
            world_id="world",
            body_id=route_body,
            character_id="luotianyi",
            provision_id=INTEGRATED_PROVISION,
            server_install_id="server-test-1",
            client_install_id="client-test-1",
        ),
        role_owner=roles,
        epoch_store=None,
        epoch_worker=worker,
        commands=commands,
        stage=stage,
        monotonic_now=lambda: 10.0,
        utc_now=lambda: NOW,
    )
    transport = MinecraftV2AuthenticatedTransport(
        peers=peers,
        hub_install_id="hub-test-1",
        monotonic_millis=clock,
        utc_now=lambda: NOW,
        nonce=lambda: HUB_NONCE,
        challenge_id=lambda: CHALLENGE_ID,
        role_owner=roles,
        binding_coordinator=coordinator,
        epoch_worker=worker,
    )
    server, controller = Socket(peers[0], candidate=True), Socket(peers[1], business=True)
    tasks = [asyncio.create_task(transport.handle(server)), asyncio.create_task(transport.handle(controller))]
    try:
        deadline = asyncio.get_running_loop().time() + 2
        while not commands.sent and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0)
        assert len(commands.sent) == 1 and type(commands.sent[0]) is BindingPrepare
        assert coordinator.pending_count == 1
        pending = next(iter(coordinator._pending.values()))
        assert pending.phase is BindingPhase.PREPARE and pending.committed is None
        assert coordinator.active is None and coordinator.ingress is None
        assert stage.minecraft_observation_scope is None
        assert len(transport._admissions) <= 1 and worker.outstanding_count == 1
        assert not any(type(command) is BindingCommit for command in commands.sent)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await worker.close()

    reopened = MinecraftEpochStoreWorker(
        EpochWorkerConfig(mode="OPEN", directory=epoch_directory, platform=AdmissionPlatform())
    )
    await reopened.start()
    try:
        handle = reopened.submit_preflight(
            EpochPreflightRequest(
                provision_id=INTEGRATED_PROVISION,
                body_id=route_body,
                coordinator_generation=0,
                route_id="30000000-0000-4000-8000-000000000003",
                server_connection_id="40000000-0000-4000-8000-000000000004",
                controller_connection_id="50000000-0000-4000-8000-000000000005",
            )
        )
        await reopened.wait(handle)
        snapshot = reopened.take_preflight(handle)
        assert snapshot.expected_last_epoch == 0
        assert snapshot.expected_generation == 0
        assert snapshot.next_epoch == 1
        reopened.mark_obsolete(snapshot)
        assert reopened.outstanding_count == 0
    finally:
        await reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("withdraw_index", (0, 1))
async def test_real_admission_blocked_preflight_is_cancelled_by_either_role_withdrawal(
    peers, tmp_path, monkeypatch, withdraw_index
):
    clock = Clock()
    route_body = "11111111-2222-4333-8444-555555555553"
    roles = AuthenticatedControlRoleOwner(tuple(peer.context for peer in peers), "hub-test-1")
    entered = threading.Event()
    release = threading.Event()
    original_read = EpochStore.read_candidate

    def gated_read(store):
        entered.set()
        release.wait(timeout=5)
        return original_read(store)

    monkeypatch.setattr(EpochStore, "read_candidate", gated_read)
    worker = MinecraftEpochStoreWorker(
        EpochWorkerConfig(
            mode="PROVISION",
            directory=(tmp_path / "epoch").resolve(),
            platform=AdmissionPlatform(),
            provision_id=INTEGRATED_PROVISION,
            body_id=route_body,
        )
    )
    await worker.start()
    commands = BindingCommands()
    coordinator = MinecraftControlBindingCoordinator(
        policy=ControlBindingPolicy(
            actor_id="actor",
            world_id="world",
            body_id=route_body,
            character_id="luotianyi",
            provision_id=INTEGRATED_PROVISION,
            server_install_id="server-test-1",
            client_install_id="client-test-1",
        ),
        role_owner=roles,
        epoch_store=None,
        epoch_worker=worker,
        commands=commands,
        stage=IntegratedStage(),
        monotonic_now=lambda: 10.0,
        utc_now=lambda: NOW,
    )
    transport = MinecraftV2AuthenticatedTransport(
        peers=peers,
        hub_install_id="hub-test-1",
        monotonic_millis=clock,
        utc_now=lambda: NOW,
        nonce=lambda: HUB_NONCE,
        challenge_id=lambda: CHALLENGE_ID,
        role_owner=roles,
        binding_coordinator=coordinator,
        epoch_worker=worker,
    )
    sockets = [Socket(peers[0], candidate=True), Socket(peers[1], business=True)]
    handles = [asyncio.create_task(transport.handle(socket)) for socket in sockets]
    try:
        deadline = asyncio.get_running_loop().time() + 2
        while not entered.is_set() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0)
        assert entered.is_set()
        deadline = asyncio.get_running_loop().time() + 2
        while len(transport._admissions) != 1 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0)
        assert len(transport._admissions) == 1 and worker.outstanding_count == 1
        admission = transport._admissions[0].task
        handles[withdraw_index].cancel()
        await asyncio.wait_for(asyncio.gather(handles[withdraw_index], return_exceptions=True), timeout=2)
        deadline = asyncio.get_running_loop().time() + 2
        while not admission.done() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0)
        assert admission.done() and admission.cancelled()
        assert coordinator.pending_count == 0 and commands.sent == []
        assert worker.outstanding_count == 1
        assert roles.current("SERVER_AUTHORITY" if withdraw_index == 0 else "CLIENT_CONTROLLER") is None
        release.set()
        deadline = asyncio.get_running_loop().time() + 2
        while worker.outstanding_count and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0)
        assert worker.outstanding_count == 0 and transport._admissions == []
    finally:
        release.set()
        for handle in handles:
            if not handle.done():
                handle.cancel()
        await asyncio.gather(*handles, return_exceptions=True)
        await worker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("withdraw_index", (0, 1))
async def test_real_admission_blocked_prepare_send_is_stale_for_either_role(peers, tmp_path, withdraw_index):
    clock = Clock()
    route_body = "11111111-2222-4333-8444-555555555553"
    roles = AuthenticatedControlRoleOwner(tuple(peer.context for peer in peers), "hub-test-1")
    worker = MinecraftEpochStoreWorker(
        EpochWorkerConfig(
            mode="PROVISION",
            directory=(tmp_path / "epoch").resolve(),
            platform=AdmissionPlatform(),
            provision_id=INTEGRATED_PROVISION,
            body_id=route_body,
        )
    )
    await worker.start()
    commands = BlockingPrepareCommands()
    coordinator = MinecraftControlBindingCoordinator(
        policy=ControlBindingPolicy(
            actor_id="actor",
            world_id="world",
            body_id=route_body,
            character_id="luotianyi",
            provision_id=INTEGRATED_PROVISION,
            server_install_id="server-test-1",
            client_install_id="client-test-1",
        ),
        role_owner=roles,
        epoch_store=None,
        epoch_worker=worker,
        commands=commands,
        stage=IntegratedStage(),
        monotonic_now=lambda: 10.0,
        utc_now=lambda: NOW,
    )
    transport = MinecraftV2AuthenticatedTransport(
        peers=peers,
        hub_install_id="hub-test-1",
        monotonic_millis=clock,
        utc_now=lambda: NOW,
        nonce=lambda: HUB_NONCE,
        challenge_id=lambda: CHALLENGE_ID,
        role_owner=roles,
        binding_coordinator=coordinator,
        epoch_worker=worker,
    )
    sockets = [Socket(peers[0], candidate=True), Socket(peers[1], business=True)]
    handles = [asyncio.create_task(transport.handle(socket)) for socket in sockets]
    try:
        await asyncio.wait_for(commands.entered.wait(), timeout=2)
        assert len(commands.sent) == 1 and len(transport._admissions) == 1
        admission = transport._admissions[0].task
        handles[withdraw_index].cancel()
        await asyncio.wait_for(asyncio.gather(handles[withdraw_index], return_exceptions=True), timeout=2)
        assert coordinator.pending_count == 0 and coordinator._v2_tickets == {}
        assert coordinator.quarantined and admission.done()
        assert len(commands.sent) == 1 and commands.cancelled == 1
        assert coordinator.active is None and coordinator.ingress is None
        commands.release.set()
        await asyncio.sleep(0)
        assert transport._admissions == []
    finally:
        commands.release.set()
        commands.revoke_release.set()
        for handle in handles:
            if not handle.done():
                handle.cancel()
        await asyncio.gather(*handles, return_exceptions=True)
        await worker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_unstarted", (False, True))
async def test_actual_candidate_with_missing_or_unavailable_worker_stays_observational(
    peers, tmp_path, explicit_unstarted
):
    clock = Clock()
    roles = AuthenticatedControlRoleOwner(tuple(peer.context for peer in peers), "hub-test-1")
    worker = (
        MinecraftEpochStoreWorker(EpochWorkerConfig(mode="OPEN", directory=(tmp_path / "missing").resolve()))
        if explicit_unstarted
        else None
    )
    if worker is not None:
        with pytest.raises(Exception):
            await worker.start()
    commands = BindingCommands()
    coordinator = _coordinator(roles, commands=commands, store=None, worker=worker)
    transport = MinecraftV2AuthenticatedTransport(
        peers=peers,
        hub_install_id="hub-test-1",
        monotonic_millis=clock,
        utc_now=lambda: NOW,
        nonce=lambda: HUB_NONCE,
        challenge_id=lambda: CHALLENGE_ID,
        role_owner=roles,
        binding_coordinator=coordinator,
        epoch_worker=worker,
    )
    sockets = [Socket(peers[0], candidate=True), Socket(peers[1], business=True)]
    handles = [asyncio.create_task(transport.handle(socket)) for socket in sockets]
    try:
        deadline = asyncio.get_running_loop().time() + 2
        while not transport._body_routes.candidates() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0)
        assert len(transport._body_routes.candidates()) == 1
        assert transport._admissions == [] and commands.sent == []
        assert coordinator.pending_count == 0 and coordinator._v2_tickets == {}
        assert worker is None or worker._state.value == "UNAVAILABLE"
    finally:
        for handle in handles:
            handle.cancel()
        await asyncio.gather(*handles, return_exceptions=True)
        if worker is not None:
            await worker.close()


@pytest.mark.asyncio
async def test_actual_third_candidate_at_admission_capacity_is_rejected_without_replacing_entries(
    peers, tmp_path, monkeypatch
):
    admission_creations = []
    original_create_task = asyncio.create_task

    def observe_create_task(coro, *, name=None, context=None):
        if name == "minecraft-v2-headless-admission":
            admission_creations.append(coro)
        if context is None:
            return original_create_task(coro, name=name)
        return original_create_task(coro, name=name, context=context)

    monkeypatch.setattr(asyncio, "create_task", observe_create_task)
    clock = Clock()
    route_body = "11111111-2222-4333-8444-555555555553"
    roles = AuthenticatedControlRoleOwner(tuple(peer.context for peer in peers), "hub-test-1")
    worker = MinecraftEpochStoreWorker(
        EpochWorkerConfig(
            mode="PROVISION",
            directory=(tmp_path / "epoch").resolve(),
            platform=AdmissionPlatform(),
            provision_id=INTEGRATED_PROVISION,
            body_id=route_body,
        )
    )
    await worker.start()
    commands = CapacityPrepareCommands()
    coordinator = MinecraftControlBindingCoordinator(
        policy=ControlBindingPolicy(
            actor_id="actor",
            world_id="world",
            body_id=route_body,
            character_id="luotianyi",
            provision_id=INTEGRATED_PROVISION,
            server_install_id="server-test-1",
            client_install_id="client-test-1",
        ),
        role_owner=roles,
        epoch_store=None,
        epoch_worker=worker,
        commands=commands,
        stage=IntegratedStage(),
        monotonic_now=lambda: 10.0,
        utc_now=lambda: NOW,
    )
    transport = MinecraftV2AuthenticatedTransport(
        peers=peers,
        hub_install_id="hub-test-1",
        monotonic_millis=clock,
        utc_now=lambda: NOW,
        nonce=lambda: HUB_NONCE,
        challenge_id=lambda: CHALLENGE_ID,
        role_owner=roles,
        binding_coordinator=coordinator,
        epoch_worker=worker,
    )
    server, controller = Socket(peers[0], candidate=True), Socket(peers[1], business=True)
    handles = [asyncio.create_task(transport.handle(server)), asyncio.create_task(transport.handle(controller))]
    invalidated = []
    original_invalidate = transport._body_routes.invalidate_admission

    def observe_invalidation(receipt):
        invalidated.append((receipt, tuple(transport._admissions), worker.outstanding_count))
        original_invalidate(receipt)

    monkeypatch.setattr(transport._body_routes, "invalidate_admission", observe_invalidation)
    try:
        await asyncio.wait_for(commands.entered.wait(), timeout=2)
        assert len(commands.sent) == 1 and len(transport._admissions) == 1
        commands.entered.clear()
        await controller.queue_notice()
        await asyncio.wait_for(commands.entered.wait(), timeout=2)
        first_entries = tuple(transport._admissions)
        assert len(first_entries) == 2 and worker.outstanding_count == 2
        assert all(not entry.task.done() for entry in first_entries)

        for _ in range(5):
            clock.value += 1_000
            await server.queue_heartbeat()
            await controller.queue_heartbeat()
            for _ in range(10):
                await asyncio.sleep(0)
            assert all(not handle.done() for handle in handles)
        await controller.queue_notice()

        deadline = asyncio.get_running_loop().time() + 2
        while not invalidated and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0)
        assert len(invalidated) == 1
        third_receipt, entries_at_rejection, outstanding_at_rejection = invalidated[0]
        assert entries_at_rejection == first_entries
        assert tuple(transport._admissions) == first_entries
        assert len(entries_at_rejection) == 2 and outstanding_at_rejection == 2
        assert len(admission_creations) == 2
        assert all(entry.receipt is not third_receipt for entry in first_entries)
        assert transport._body_routes._record_for_receipt(third_receipt) is None
        assert len(commands.sent) == 2
    finally:
        commands.release.set()
        for handle in handles:
            if not handle.done():
                handle.cancel()
        await asyncio.gather(*handles, return_exceptions=True)
        await worker.close()


def _manifest(role, peer_id):
    return DeploymentManifest(
        schema="luosandbox.minecraft-control-deployment.v2",
        security="luosandbox.control-auth.v1",
        peer_role=role,
        peer_install_id=peer_id,
        hub_install_id="hub-test-1",
        protocol="LUO_CONTROL_POSTAUTH_V2",
        post_auth_profile="LUO_CONTROL_POSTAUTH_V2",
        session_kdf_domain="LUO_CONTROL_SESSION_KEY_V2",
        frame_mac_domain="LUO_CONTROL_FRAME_V2",
        frame_max_utf8_bytes=4096,
        payload_max_raw_bytes=2048,
        ascii_id_max_bytes=128,
        outbound_queue_frames=8,
        open_timeout_ms=5000,
        heartbeat_interval_ms=500,
        heartbeat_timeout_ms=2000,
        route_capacity=2,
        route_ttl_ms=5000,
        binding_pending_capacity=8,
        store_outstanding_capacity=2,
        mod_manifest_sha256=sha256(MOD).hexdigest(),
        policy_config_sha256=sha256(POLICY).hexdigest(),
        binding_manifest_sha256=sha256(BINDING).hexdigest(),
    )


def _protect_owner_only(handle, owner_sid):
    ace_size = 8 + len(owner_sid)
    acl_size = 8 + ace_size
    acl = ctypes.create_string_buffer(
        struct.pack("<BBHHH", 2, 0, acl_size, 1, 0) + struct.pack("<BBHI", 0, 0, ace_size, 0x001F01FF) + owner_sid
    )
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.SetSecurityInfo.argtypes = (
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    advapi.SetSecurityInfo.restype = ctypes.c_uint
    assert advapi.SetSecurityInfo(handle, 1, 0x4 | 0x80000000, None, None, ctypes.cast(acl, ctypes.c_void_p), None) == 0


def _native_peer(tmp_path: Path, role: str):
    root = tmp_path / role.lower() / "trusted"
    root.mkdir(parents=True)
    manifest = _manifest(role, "server-test-1" if role == "SERVER_AUTHORITY" else "client-test-1")
    paths = [root / name for name in ("deployment.json", "mod.json", "policy.json", "binding.json")]
    for path, raw in zip(paths, (manifest.raw_bytes, MOD, POLICY, BINDING)):
        path.write_bytes(raw)
    files = DeploymentFileProvision(
        deployment_manifest_path=paths[0],
        mod_manifest_path=paths[1],
        policy_config_path=paths[2],
        binding_manifest_path=paths[3],
        trusted_identity=TrustedDeploymentIdentity(
            peer_role=manifest.peer_role,
            peer_install_id=manifest.peer_install_id,
            hub_install_id=manifest.hub_install_id,
            expected_config_hash=manifest.config_hash,
        ),
    )
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_void_p,
    )
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel.CloseHandle.restype = ctypes.c_int
    root_handle = kernel.CreateFileW(
        str(root), 0x00020000 | 0x00040000 | 0x80, 1, None, 3, 0x02000000 | 0x00200000, None
    )
    assert root_handle not in (None, ctypes.c_void_p(-1).value)
    try:
        identity = _file_info(kernel, root_handle)
        descriptor = inspect_handle_security_descriptor(root_handle)
        filesystem_name = ctypes.create_unicode_buffer(261)
        kernel.GetVolumeInformationByHandleW.argtypes = (
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
            ctypes.c_wchar_p,
            ctypes.c_uint,
        )
        kernel.GetVolumeInformationByHandleW.restype = ctypes.c_int
        assert kernel.GetVolumeInformationByHandleW(
            root_handle, None, 0, None, None, None, filesystem_name, len(filesystem_name)
        )
        if filesystem_name.value.casefold() != "ntfs":
            pytest.skip(f"verified v2 transport requires NTFS, found {filesystem_name.value!r}")
    finally:
        assert kernel.CloseHandle(root_handle)
    for path in paths:
        handle = kernel.CreateFileW(str(path), 0x00020000 | 0x00040000, 7, None, 3, 0x80 | 0x00200000, None)
        assert handle not in (None, ctypes.c_void_p(-1).value)
        try:
            _protect_owner_only(handle, descriptor.owner_sid)
        finally:
            assert kernel.CloseHandle(handle)
    root_handle = kernel.CreateFileW(
        str(root), 0x00020000 | 0x00040000 | 0x80, 1, None, 3, 0x02000000 | 0x00200000, None
    )
    assert root_handle not in (None, ctypes.c_void_p(-1).value)
    try:
        _protect_owner_only(root_handle, descriptor.owner_sid)
    finally:
        assert kernel.CloseHandle(root_handle)
    verified = load_verified_deployment_filesystem(
        files,
        TrustedDeploymentRoot(
            root_path=root,
            expected_volume_serial=identity.volume_serial,
            expected_file_index=identity.file_index,
            acl_policy=TrustedDeploymentAclPolicy(
                expected_owner_sid=descriptor.owner_sid,
                allowed_writer_sids=(descriptor.owner_sid,),
            ),
        ),
    )
    return provision_v2_peer(
        verified,
        expected_peer_role=manifest.peer_role,
        expected_peer_install_id=manifest.peer_install_id,
        expected_hub_install_id=manifest.hub_install_id,
        expected_config_hash=manifest.config_hash,
        key_id=f"{role.lower()}-key",
        role_psk=PSK,
    )


@pytest.fixture
def peers(tmp_path):
    values = (
        _native_peer(tmp_path, "SERVER_AUTHORITY"),
        _native_peer(tmp_path, "CLIENT_CONTROLLER"),
    )
    try:
        yield values
    finally:
        for peer in values:
            peer.close()


def _hello(peer, **changes):
    value = {
        "security": "luosandbox.control-auth.v1",
        "kind": "HELLO",
        "peerRole": peer.context.peer_role,
        "peerInstallId": peer.context.peer_install_id,
        "hubInstallId": peer.context.hub_install_id,
        "keyId": peer.context.key_id,
        "protocol": peer.context.protocol,
        "modHash": b32_encode(peer.context.mod_hash),
        "configHash": b32_encode(peer.context.config_hash),
        "bindingHash": b32_encode(peer.context.binding_hash),
        "peerNonce": b32_encode(PEER_NONCE),
    }
    value.update(changes)
    return json.dumps(value, separators=(",", ":"))


class Socket:
    def __init__(
        self,
        peer,
        *,
        hello=None,
        wrong_proof=False,
        fail_send_at=None,
        business=False,
        candidate=False,
        business_count=1,
    ):
        self.peer = peer
        self.messages = asyncio.Queue()
        self.messages.put_nowait(hello or _hello(peer))
        self.wrong_proof = wrong_proof
        self.fail_send_at = fail_send_at
        self.business = business
        self.candidate = candidate
        self.business_count = business_count
        self.next_peer_sequence = 1
        self.sent = []
        self.closed = None
        self.headers = {}
        self.client = None
        self.receive_active = self.receive_highwater = 0
        self.send_active = self.send_highwater = 0
        self.keys = None
        self.authenticated = None
        self.connection_id = None
        self.close_failure = False
        self.close_entered = asyncio.Event()
        self.close_release = asyncio.Event()
        self.block_close = False

    async def accept(self, *, subprotocol=None):
        self.accepted = subprotocol

    async def receive_text(self):
        self.receive_active += 1
        self.receive_highwater = max(self.receive_highwater, self.receive_active)
        try:
            return await self.messages.get()
        finally:
            self.receive_active -= 1

    async def send_text(self, data):
        self.send_active += 1
        self.send_highwater = max(self.send_highwater, self.send_active)
        try:
            if self.fail_send_at == len(self.sent):
                raise RuntimeError("send failed")
            value = json.loads(data)
            self.sent.append(value)
            if value["kind"] == "CHALLENGE":
                issued = datetime.fromisoformat(value["issuedAt"].replace("Z", "+00:00"))
                expires = datetime.fromisoformat(value["expiresAt"].replace("Z", "+00:00"))
                proof = compute_proof(
                    PSK,
                    "PEER_PROOF",
                    self.peer.context,
                    PEER_NONCE,
                    HUB_NONCE,
                    value["challengeId"],
                    issued,
                    expires,
                )
                await self.messages.put(
                    json.dumps(
                        {
                            "security": "luosandbox.control-auth.v1",
                            "kind": "PROOF",
                            "challengeId": value["challengeId"],
                            "peerProof": "A" * 43 if self.wrong_proof else proof,
                        },
                        separators=(",", ":"),
                    )
                )
            elif value["kind"] == "READY":
                challenge = self.sent[0]
                issued = datetime.fromisoformat(challenge["issuedAt"].replace("Z", "+00:00"))
                expires = datetime.fromisoformat(challenge["expiresAt"].replace("Z", "+00:00"))
                self.authenticated = AuthenticatedTranscript(
                    context=self.peer.context,
                    peer_nonce=PEER_NONCE,
                    hub_nonce=HUB_NONCE,
                    challenge_id=challenge["challengeId"],
                    issued_at=issued,
                    expires_at=expires,
                )
                self.keys = PostAuthSessionKeys(self.authenticated, PSK)
            elif value["kind"] == "AUTHENTICATED_FRAME":
                self.connection_id = value["connectionId"]
                context = TrustedFrameContext(authenticated=self.authenticated, connection_id=self.connection_id)
                if value["messageKind"] == "SESSION_OPEN":
                    ack = PostAuthFrame(
                        connection_id=self.connection_id,
                        challenge_id=self.authenticated.challenge_id,
                        sequence=0,
                        message_kind="HEARTBEAT",
                        route_id="",
                        source_install_id="",
                        source_connection_id="",
                        recipient_connection_id="",
                        challenge_expires_at="",
                        payload=b"{}",
                    )
                    await self.messages.put(encode_frame(ack, context, self.keys, "PEER_TO_HUB").decode("ascii"))
                if self.business and value["messageKind"] == "SESSION_OPEN":
                    for _ in range(self.business_count):
                        await self.queue_notice()
                if value["messageKind"] == "BODY_BIND_ROUTE" and self.candidate:
                    await self.queue_candidate(value, context)
        finally:
            self.send_active -= 1

    async def queue_notice(self):
        context = TrustedFrameContext(authenticated=self.authenticated, connection_id=self.connection_id)
        sequence = self.next_peer_sequence
        self.next_peer_sequence += 1
        frame = PostAuthFrame(
            connection_id=self.connection_id,
            challenge_id=self.authenticated.challenge_id,
            sequence=sequence,
            message_kind="BODY_BIND_NOTICE",
            route_id="",
            source_install_id="",
            source_connection_id="",
            recipient_connection_id="",
            challenge_expires_at="",
            payload=json.dumps(
                {
                    "security": "luosandbox.control-auth.v1",
                    "kind": "BODY_BIND_NOTICE",
                    "serverBootId": "11111111-2222-4333-8444-555555555551",
                    "gameConnectionId": "11111111-2222-4333-8444-555555555552",
                    "bodyId": "11111111-2222-4333-8444-555555555553",
                    "gameNonce": b32_encode(b"n" * 32),
                },
                separators=(",", ":"),
            ).encode(),
        )
        await self.messages.put(encode_frame(frame, context, self.keys, "PEER_TO_HUB").decode("ascii"))

    async def queue_candidate(self, value, context):
        sequence = self.next_peer_sequence
        self.next_peer_sequence += 1
        routed_payload = base64.urlsafe_b64decode(value["payload"] + "=" * (-len(value["payload"]) % 4))
        notice_value = json.loads(routed_payload)
        candidate_payload = json.dumps(
            {
                "security": "luosandbox.control-auth.v1",
                "kind": "BODY_BOUND",
                "serverBootId": notice_value["serverBootId"],
                "gameConnectionId": notice_value["gameConnectionId"],
                "bodyId": notice_value["bodyId"],
            },
            separators=(",", ":"),
        ).encode()
        candidate_frame = PostAuthFrame(
            connection_id=self.connection_id,
            challenge_id=self.authenticated.challenge_id,
            sequence=sequence,
            message_kind="BODY_BOUND_CANDIDATE",
            route_id=value["routeId"],
            source_install_id="",
            source_connection_id="",
            recipient_connection_id=self.connection_id,
            challenge_expires_at="2026-01-02T03:04:10.000Z",
            payload=candidate_payload,
        )
        await self.messages.put(encode_frame(candidate_frame, context, self.keys, "PEER_TO_HUB").decode("ascii"))

    async def queue_heartbeat(self):
        context = TrustedFrameContext(authenticated=self.authenticated, connection_id=self.connection_id)
        sequence = self.next_peer_sequence
        self.next_peer_sequence += 1
        frame = PostAuthFrame(
            connection_id=self.connection_id,
            challenge_id=self.authenticated.challenge_id,
            sequence=sequence,
            message_kind="HEARTBEAT",
            route_id="",
            source_install_id="",
            source_connection_id="",
            recipient_connection_id="",
            challenge_expires_at="",
            payload=b"{}",
        )
        await self.messages.put(encode_frame(frame, context, self.keys, "PEER_TO_HUB").decode("ascii"))

    async def close(self, code=1000, reason=None):
        self.close_entered.set()
        if self.block_close:
            await self.close_release.wait()
        self.closed = (code, reason)
        if self.keys is not None:
            self.keys.close()
        if self.close_failure:
            raise RuntimeError("close failed")


def _transport(peers, clock):
    return MinecraftV2AuthenticatedTransport(
        peers=peers,
        hub_install_id="hub-test-1",
        monotonic_millis=clock,
        utc_now=lambda: NOW,
        nonce=lambda: HUB_NONCE,
        challenge_id=lambda: CHALLENGE_ID,
    )


def test_constructor_rejects_raw_closed_duplicate_role_and_wrong_hub(peers):
    clock = Clock()
    with pytest.raises(ValueError):
        MinecraftV2AuthenticatedTransport(
            peers=(object(),), hub_install_id="hub-test-1", monotonic_millis=clock, utc_now=lambda: NOW
        )
    with pytest.raises(ValueError):
        MinecraftV2AuthenticatedTransport(
            peers=(peers[0], peers[0]), hub_install_id="hub-test-1", monotonic_millis=clock, utc_now=lambda: NOW
        )
    with pytest.raises(ValueError):
        MinecraftV2AuthenticatedTransport(
            peers=peers, hub_install_id="wrong-hub", monotonic_millis=clock, utc_now=lambda: NOW
        )
    peers[0].close()
    with pytest.raises(ValueError):
        MinecraftV2AuthenticatedTransport(
            peers=(peers[0],), hub_install_id="hub-test-1", monotonic_millis=clock, utc_now=lambda: NOW
        )


def test_constructor_accepts_exact_shared_owner_and_rejects_context_mismatch(peers):
    clock = Clock()
    owner = AuthenticatedControlRoleOwner(tuple(peer.context for peer in peers), "hub-test-1")
    transport = MinecraftV2AuthenticatedTransport(
        peers=peers,
        hub_install_id="hub-test-1",
        monotonic_millis=clock,
        utc_now=lambda: NOW,
        role_owner=owner,
    )
    assert transport._roles is owner and owner._loop is None
    other = AuthenticatedControlRoleOwner((peers[0].context,), "hub-test-1")
    with pytest.raises(ValueError, match="provisioning mismatch"):
        MinecraftV2AuthenticatedTransport(
            peers=peers,
            hub_install_id="hub-test-1",
            monotonic_millis=clock,
            utc_now=lambda: NOW,
            role_owner=other,
        )
    coordinator = _coordinator(owner)
    integrated = MinecraftV2AuthenticatedTransport(
        peers=peers,
        hub_install_id="hub-test-1",
        monotonic_millis=clock,
        utc_now=lambda: NOW,
        role_owner=owner,
        binding_coordinator=coordinator,
    )
    assert integrated._roles is coordinator.role_owner
    different = AuthenticatedControlRoleOwner(tuple(peer.context for peer in peers), "hub-test-1")
    with pytest.raises(ValueError, match="role owner mismatch"):
        MinecraftV2AuthenticatedTransport(
            peers=peers,
            hub_install_id="hub-test-1",
            monotonic_millis=clock,
            utc_now=lambda: NOW,
            role_owner=different,
            binding_coordinator=coordinator,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("index", (0, 1))
async def test_actual_both_roles_authenticate_open_ack_and_cleanup(index, peers):
    clock = Clock()
    owner = _transport(peers, clock)
    socket = Socket(peers[index])
    task = asyncio.create_task(owner.handle(socket))
    deadline = asyncio.get_running_loop().time() + 2
    while (not owner.active_roles or len(socket.sent) < 3) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0)
    assert owner.active_roles == (peers[index].context.peer_role,)
    assert socket.sent[0]["kind"] == "CHALLENGE" and socket.sent[1]["kind"] == "READY"
    assert socket.sent[2]["messageKind"] == "SESSION_OPEN"
    assert socket.receive_highwater <= 1 and socket.send_highwater <= 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert owner.active_roles == () and owner.pending_count == 0
    assert socket.closed == (1001, "FEATURE_DISABLED")
    assert all(peer.is_provisioned for peer in peers)


@pytest.mark.asyncio
async def test_integrated_withdrawal_fences_before_revoke_then_socket_close(peers):
    clock = Clock()
    role_owner = AuthenticatedControlRoleOwner(tuple(peer.context for peer in peers), "hub-test-1")
    commands, stage = BindingCommands(), IntegratedStage()
    commands.block_revoke = True
    coordinator = _coordinator(role_owner, commands=commands, stage=stage, store=BindingStore())
    transport = MinecraftV2AuthenticatedTransport(
        peers=peers,
        hub_install_id="hub-test-1",
        monotonic_millis=clock,
        utc_now=lambda: NOW,
        nonce=lambda: HUB_NONCE,
        challenge_id=lambda: CHALLENGE_ID,
        role_owner=role_owner,
        binding_coordinator=coordinator,
    )
    sockets = [Socket(peer) for peer in peers]
    sockets[0].block_close = True
    tasks = [asyncio.create_task(transport.handle(socket)) for socket in sockets]
    deadline = asyncio.get_running_loop().time() + 2
    while len(transport.active_roles) != 2 and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0)
    server_lease = role_owner.current("SERVER_AUTHORITY")
    server = AuthenticatedRoleConnection(
        connection_id=server_lease.connection_id,
        handshake=server_lease.handshake,
    )
    controller_lease = role_owner.current("CLIENT_CONTROLLER")
    await coordinator.prepare(
        server,
        VerifiedBodyBound(
            server_boot_id="boot",
            game_connection_id="game",
            body_id=INTEGRATED_BODY,
            client_install_id="client-test-1",
            controller_connection_id=controller_lease.connection_id,
            evidence_id="evidence",
        ),
    )
    tasks[0].cancel()
    await commands.revoke_entered.wait()
    assert role_owner.current("SERVER_AUTHORITY") is None
    assert coordinator.pending_count == 0 and coordinator.active_snapshot() is None
    assert coordinator.ingress is None and stage.minecraft_observation_scope is None
    assert coordinator.quarantined and not sockets[0].close_entered.is_set()
    commands.revoke_release.set()
    await sockets[0].close_entered.wait()
    tasks[0].cancel()
    assert role_owner.current("SERVER_AUTHORITY") is None and coordinator.pending_count == 0
    sockets[0].close_release.set()
    tasks[1].cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert transport.active_roles == () and transport.pending_count == 0


@pytest.mark.asyncio
async def test_route_invalidation_failure_still_fences_coordinator_before_socket_close(peers, monkeypatch):
    clock = Clock()
    role_owner = AuthenticatedControlRoleOwner(tuple(peer.context for peer in peers), "hub-test-1")
    commands, stage = BindingCommands(), IntegratedStage()
    commands.block_revoke = True
    coordinator = _coordinator(role_owner, commands=commands, stage=stage, store=BindingStore())
    transport = MinecraftV2AuthenticatedTransport(
        peers=peers,
        hub_install_id="hub-test-1",
        monotonic_millis=clock,
        utc_now=lambda: NOW,
        nonce=lambda: HUB_NONCE,
        challenge_id=lambda: CHALLENGE_ID,
        role_owner=role_owner,
        binding_coordinator=coordinator,
    )
    sockets = [Socket(peer) for peer in peers]
    sockets[0].block_close = True
    tasks = [asyncio.create_task(transport.handle(socket)) for socket in sockets]
    deadline = asyncio.get_running_loop().time() + 2
    while len(transport.active_roles) != 2 and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0)
    server_lease = role_owner.current("SERVER_AUTHORITY")
    server = AuthenticatedRoleConnection(
        connection_id=server_lease.connection_id,
        handshake=server_lease.handshake,
    )
    controller_lease = role_owner.current("CLIENT_CONTROLLER")
    await coordinator.prepare(
        server,
        VerifiedBodyBound(
            server_boot_id="boot",
            game_connection_id="game",
            body_id=INTEGRATED_BODY,
            client_install_id="client-test-1",
            controller_connection_id=controller_lease.connection_id,
            evidence_id="evidence",
        ),
    )
    monkeypatch.setattr(
        transport._body_routes,
        "invalidate_lease",
        lambda lease: (_ for _ in ()).throw(RuntimeError("route cleanup failed")),
    )
    tasks[0].cancel()
    await commands.revoke_entered.wait()
    assert role_owner.current("SERVER_AUTHORITY") is None
    assert coordinator.pending_count == 0 and coordinator.active_snapshot() is None
    assert coordinator.ingress is None and coordinator.quarantined
    assert not sockets[0].close_entered.is_set()
    commands.revoke_release.set()
    await sockets[0].close_entered.wait()
    sockets[0].close_release.set()
    tasks[1].cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert any(isinstance(result, asyncio.CancelledError) for result in results)
    assert transport.active_roles == () and transport.pending_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("withdraw_index", (0, 1))
async def test_matching_admission_is_cancelled_and_drained_when_route_invalidation_fails(
    peers, monkeypatch, withdraw_index
):
    clock = Clock()
    role_owner = AuthenticatedControlRoleOwner(tuple(peer.context for peer in peers), "hub-test-1")
    commands, stage = BindingCommands(), IntegratedStage()
    commands.block_revoke = True
    coordinator = _coordinator(role_owner, commands=commands, stage=stage, store=BindingStore())
    transport = MinecraftV2AuthenticatedTransport(
        peers=peers,
        hub_install_id="hub-test-1",
        monotonic_millis=clock,
        utc_now=lambda: NOW,
        nonce=lambda: HUB_NONCE,
        challenge_id=lambda: CHALLENGE_ID,
        role_owner=role_owner,
        binding_coordinator=coordinator,
    )
    sockets = [Socket(peer) for peer in peers]
    sockets[withdraw_index].block_close = True
    handles = [asyncio.create_task(transport.handle(socket)) for socket in sockets]
    admission = None
    try:
        deadline = asyncio.get_running_loop().time() + 2
        while len(transport.active_roles) != 2 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0)
        server_lease = role_owner.current("SERVER_AUTHORITY")
        controller_lease = role_owner.current("CLIENT_CONTROLLER")
        server = AuthenticatedRoleConnection(
            connection_id=server_lease.connection_id,
            handshake=server_lease.handshake,
        )
        await coordinator.prepare(
            server,
            VerifiedBodyBound(
                server_boot_id="boot",
                game_connection_id="game",
                body_id=INTEGRATED_BODY,
                client_install_id="client-test-1",
                controller_connection_id=controller_lease.connection_id,
                evidence_id="evidence",
            ),
        )
        admission_entered = asyncio.Event()
        admission_finished = asyncio.Event()

        async def blocked_admission():
            admission_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                admission_finished.set()

        admission = asyncio.create_task(blocked_admission())
        await admission_entered.wait()
        transport._admissions.append(
            SimpleNamespace(
                task=admission,
                server_lease=server_lease,
                controller_lease=controller_lease,
                receipt=object(),
            )
        )
        attempts = []

        def fail_invalidation(lease):
            attempts.append(lease)
            raise RuntimeError("route cleanup failed")

        monkeypatch.setattr(transport._body_routes, "invalidate_lease", fail_invalidation)
        handles[withdraw_index].cancel()
        await asyncio.wait_for(commands.revoke_entered.wait(), timeout=2)
        await asyncio.wait_for(admission_finished.wait(), timeout=2)
        assert coordinator.pending_count == 0 and coordinator.quarantined
        assert admission.cancelled() and admission.done()
        assert not sockets[withdraw_index].close_entered.is_set()
        assert attempts == [server_lease if withdraw_index == 0 else controller_lease]
        commands.revoke_release.set()
        await asyncio.wait_for(sockets[withdraw_index].close_entered.wait(), timeout=2)
        sockets[withdraw_index].close_release.set()
        result = await asyncio.gather(handles[withdraw_index], return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
    finally:
        commands.revoke_release.set()
        sockets[withdraw_index].close_release.set()
        if admission is not None and not admission.done():
            admission.cancel()
            await asyncio.gather(admission, return_exceptions=True)
        for handle in handles:
            if not handle.done():
                handle.cancel()
        await asyncio.gather(*handles, return_exceptions=True)


@pytest.mark.asyncio
async def test_protocol_close_reports_sanitized_route_cleanup_failure_after_all_cleanup(peers, monkeypatch):
    clock = Clock()
    role_owner = AuthenticatedControlRoleOwner(tuple(peer.context for peer in peers), "hub-test-1")
    commands, stage = BindingCommands(), IntegratedStage()
    commands.block_revoke = True
    coordinator = _coordinator(role_owner, commands=commands, stage=stage, store=BindingStore())
    transport = MinecraftV2AuthenticatedTransport(
        peers=peers,
        hub_install_id="hub-test-1",
        monotonic_millis=clock,
        utc_now=lambda: NOW,
        nonce=lambda: HUB_NONCE,
        challenge_id=lambda: CHALLENGE_ID,
        role_owner=role_owner,
        binding_coordinator=coordinator,
    )
    sockets = [Socket(peer) for peer in peers]
    sockets[0].block_close = True
    tasks = [asyncio.create_task(transport.handle(socket)) for socket in sockets]
    deadline = asyncio.get_running_loop().time() + 2
    while len(transport.active_roles) != 2 and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0)
    server_lease = role_owner.current("SERVER_AUTHORITY")
    server = AuthenticatedRoleConnection(
        connection_id=server_lease.connection_id,
        handshake=server_lease.handshake,
    )
    controller_lease = role_owner.current("CLIENT_CONTROLLER")
    await coordinator.prepare(
        server,
        VerifiedBodyBound(
            server_boot_id="boot",
            game_connection_id="game",
            body_id=INTEGRATED_BODY,
            client_install_id="client-test-1",
            controller_connection_id=controller_lease.connection_id,
            evidence_id="evidence",
        ),
    )
    attempts = []

    def fail_invalidation(lease):
        attempts.append(lease)
        raise RuntimeError("raw cleanup backend detail")

    monkeypatch.setattr(transport._body_routes, "invalidate_lease", fail_invalidation)
    await sockets[0].messages.put("{}")
    await commands.revoke_entered.wait()
    assert role_owner.current("SERVER_AUTHORITY") is None
    assert coordinator.pending_count == 0 and coordinator.active_snapshot() is None
    assert coordinator.ingress is None and coordinator.quarantined
    assert not sockets[0].close_entered.is_set() and attempts == [server_lease]
    commands.revoke_release.set()
    await sockets[0].close_entered.wait()
    sockets[0].close_release.set()
    with pytest.raises(V2TransportError, match="^v2 transport cleanup failed$") as raised:
        await tasks[0]
    assert raised.value.__cause__ is None
    assert "raw cleanup backend detail" not in str(raised.value)
    tasks[1].cancel()
    await asyncio.gather(tasks[1], return_exceptions=True)
    assert attempts.count(server_lease) == 1
    assert sockets[0].closed == (1008, "FEATURE_DISABLED")
    assert transport.active_roles == () and transport.pending_count == 0


@pytest.mark.asyncio
async def test_actual_transport_routes_notice_to_server_and_accepts_only_candidate_dto(peers):
    clock = Clock()
    transport = _transport(peers, clock)
    server = Socket(peers[0], candidate=True)
    controller = Socket(peers[1], business=True)
    server_task = asyncio.create_task(transport.handle(server))
    deadline = asyncio.get_running_loop().time() + 2
    while (
        "SERVER_AUTHORITY" not in transport._sessions
        or transport._sessions["SERVER_AUTHORITY"][1].state is not PostAuthSessionState.APPLICATION_READY
    ) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0)
    controller_task = asyncio.create_task(transport.handle(controller))
    while not transport._body_routes.candidates() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0)
    candidates = transport._body_routes.candidates()
    assert len(candidates) == 1 and candidates[0].body_id == "11111111-2222-4333-8444-555555555553"
    route_frames = [item for item in server.sent if item.get("messageKind") == "BODY_BIND_ROUTE"]
    assert len(route_frames) == 1
    route = route_frames[0]
    assert route["sourceInstallId"] == "client-test-1"
    assert route["sourceConnectionId"] == controller.connection_id
    assert route["recipientConnectionId"] == server.connection_id
    assert route["challengeExpiresAt"] == ""
    assert controller.closed is None and transport._binding is None
    server_task.cancel()
    controller_task.cancel()
    await asyncio.gather(server_task, controller_task, return_exceptions=True)
    assert transport.active_roles == () and transport._body_routes.candidates() == ()


@pytest.mark.asyncio
async def test_wrong_hello_proof_extras_depth_and_oversize_fail_closed(peers):
    bad = (
        _hello(peers[0], protocol="luosandbox.minecraft.v1"),
        _hello(peers[0], peerNonce=b32_encode(b"\0" * 32)),
        _hello(peers[0])[:-1] + ',"extra":1}',
        '{"security":"luosandbox.control-auth.v1","kind":"HELLO","kind":"HELLO"}',
        json.dumps({"a": {"b": {"c": {"d": {"e": 1}}}}}),
        "x" * 4097,
    )
    for hello in bad:
        owner = _transport(peers, Clock())
        socket = Socket(peers[0], hello=hello)
        await owner.handle(socket)
        assert socket.closed == (1008, "FEATURE_DISABLED") and owner.active_roles == ()
    socket = Socket(peers[0], wrong_proof=True)
    owner = _transport(peers, Clock())
    await owner.handle(socket)
    assert socket.closed == (1008, "FEATURE_DISABLED") and owner.active_roles == ()


@pytest.mark.asyncio
async def test_ready_or_session_open_send_failure_never_leaves_role(peers):
    for send_index in (1, 2):
        owner = _transport(peers, Clock())
        socket = Socket(peers[0], fail_send_at=send_index)
        await owner.handle(socket)
        assert owner.active_roles == () and owner.pending_count == 0
        assert socket.closed == (1008, "FEATURE_DISABLED")


@pytest.mark.asyncio
async def test_business_frame_is_denied_and_cannot_retain_role(peers):
    owner = _transport(peers, Clock())
    socket = Socket(peers[1], business=True)
    await asyncio.wait_for(owner.handle(socket), timeout=2)
    assert owner.active_roles == () and owner.pending_count == 0
    assert socket.closed == (1008, "FEATURE_DISABLED")


@pytest.mark.asyncio
async def test_cancellation_drains_receive_and_does_not_close_startup_credential(peers):
    owner = _transport(peers, Clock())
    socket = Socket(peers[0])
    task = asyncio.create_task(owner.handle(socket))
    deadline = asyncio.get_running_loop().time() + 2
    while not owner.active_roles and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0)
    task.cancel()
    asyncio.get_running_loop().call_soon(task.cancel)
    await asyncio.gather(task, return_exceptions=True)
    assert socket.receive_active == socket.send_active == 0
    assert owner.active_roles == () and owner.pending_count == 0
    assert peers[0].is_provisioned


@pytest.mark.asyncio
async def test_capacity_rate_limit_duplicate_role_and_close_failure_remain_fail_closed(peers):
    clock = Clock()
    owner = _transport(peers, clock)
    sockets = [Socket(peers[0]) for _ in range(3)]
    tasks = [asyncio.create_task(owner.handle(socket)) for socket in sockets[:2]]
    deadline = asyncio.get_running_loop().time() + 2
    while not owner.active_roles and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0)
    await owner.handle(sockets[2])
    assert sockets[2].closed == (1008, "FEATURE_DISABLED")
    sockets[0].close_failure = True
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert owner.active_roles == () and owner.pending_count == 0

    limited = _transport(peers, clock)
    for _ in range(2):
        socket = Socket(peers[1], wrong_proof=True)
        await limited.handle(socket)
    third = Socket(peers[1], wrong_proof=True)
    await limited.handle(third)
    assert third.closed == (1008, "FEATURE_DISABLED") and limited.active_roles == ()


@pytest.mark.asyncio
async def test_invalid_monotonic_millis_fail_before_authentication(peers):
    for value in (True, -1, _MAX_SAFE_AUTH_MILLIS + 1):
        owner = _transport(peers, lambda value=value: value)
        socket = Socket(peers[0])
        await owner.handle(socket)
        assert socket.closed == (1008, "FEATURE_DISABLED") and owner.active_roles == ()
        assert owner.pending_count == 0 and not socket.sent


def test_safe_upper_clock_bound_preserves_adjacent_millis_and_actual_pending_deadline(peers):
    peer = peers[0]
    upper = _MAX_SAFE_AUTH_MILLIS
    assert float(upper) / 1000.0 < float(upper + 1) / 1000.0
    owner = _transport(peers, lambda: upper)
    assert owner._monotonic_millis() == upper

    connection = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    issued = upper - 5000

    def begin_pending():
        pending = PendingHandshakes()
        item, _proof = pending.begin_v2(
            connection,
            peer,
            PEER_NONCE,
            NOW,
            hub_nonce=HUB_NONCE,
            challenge_id=CHALLENGE_ID,
            now=issued / 1000.0,
            utc_now=NOW,
        )
        proof = compute_proof(
            PSK,
            "PEER_PROOF",
            peer.context,
            PEER_NONCE,
            HUB_NONCE,
            CHALLENGE_ID,
            NOW,
            NOW + timedelta(seconds=5),
        )
        return pending, item, proof

    accepted, item, proof = begin_pending()
    assert item.deadline > issued / 1000.0
    assert accepted.consume_v2(
        connection,
        CHALLENGE_ID,
        proof,
        peer,
        expected_context=peer.context,
        now=(issued + 4999) / 1000.0,
        utc_now=NOW + timedelta(seconds=4, milliseconds=999),
    ).has_full_provenance

    expired, _item, proof = begin_pending()
    with pytest.raises(ControlAuthError):
        expired.consume_v2(
            connection,
            CHALLENGE_ID,
            proof,
            peer,
            expected_context=peer.context,
            now=(issued + 5000) / 1000.0,
            utc_now=NOW + timedelta(seconds=5),
        )
