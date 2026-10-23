import asyncio
import ctypes
import json
import os
import struct
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import pytest

from src.adapter.minecraft.control_auth import ControlAuthError, PendingHandshakes, b32_encode, compute_proof
from src.adapter.minecraft.deployment_acl import TrustedDeploymentAclPolicy, inspect_handle_security_descriptor
from src.adapter.minecraft.deployment_files import (
    DeploymentFileProvision,
    TrustedDeploymentRoot,
    _file_info,
    load_verified_deployment_filesystem,
)
from src.adapter.minecraft.deployment_manifest import DeploymentManifest, TrustedDeploymentIdentity
from src.adapter.minecraft.postauth_frame import PostAuthFrame, TrustedFrameContext, encode_frame
from src.adapter.minecraft.postauth_keys import AuthenticatedTranscript, PostAuthSessionKeys
from src.adapter.minecraft.v2_startup import provision_v2_peer
from src.adapter.minecraft.v2_transport import _MAX_SAFE_AUTH_MILLIS, MinecraftV2AuthenticatedTransport

pytestmark = pytest.mark.skipif(os.name != "nt", reason="v2 transport requires native verified NTFS evidence")

MOD = b"public-mod-manifest"
POLICY = b"public-policy-config"
BINDING = b"public-binding-manifest"
PSK = bytes(range(32))
NOW = datetime(2026, 1, 2, 3, 4, 5, 678000, tzinfo=timezone.utc)
CHALLENGE_ID = "11111111-2222-4333-8444-555555555555"
PEER_NONCE = b"p" * 32
HUB_NONCE = b"h" * 32


class Clock:
    def __init__(self):
        self.value = 10_000

    def __call__(self):
        return self.value


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
    def __init__(self, peer, *, hello=None, wrong_proof=False, fail_send_at=None, business=False):
        self.peer = peer
        self.messages = asyncio.Queue()
        self.messages.put_nowait(hello or _hello(peer))
        self.wrong_proof = wrong_proof
        self.fail_send_at = fail_send_at
        self.business = business
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
                if self.business:
                    frame = PostAuthFrame(
                        connection_id=self.connection_id,
                        challenge_id=self.authenticated.challenge_id,
                        sequence=1,
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
        finally:
            self.send_active -= 1

    async def close(self, code=1000, reason=None):
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
