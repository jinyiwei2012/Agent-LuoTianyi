import copy
import ctypes
import os
import pickle
import struct
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import pytest

from src.adapter.minecraft.control_auth import ControlAuthContext, ControlAuthError, PendingHandshakes, compute_proof
from src.adapter.minecraft.deployment_acl import TrustedDeploymentAclPolicy, inspect_handle_security_descriptor
from src.adapter.minecraft.deployment_files import (
    DeploymentFileProvision,
    DeploymentFileSnapshot,
    TrustedDeploymentRoot,
    VerifiedDeploymentFilesystem,
    _file_info,
    load_unverified_deployment_snapshot,
    load_verified_deployment_filesystem,
)
from src.adapter.minecraft.deployment_manifest import DeploymentManifest, TrustedDeploymentIdentity
from src.adapter.minecraft.postauth_keys import AuthenticatedTranscript, PostAuthSessionKeys
from src.adapter.minecraft.v2_startup import ProvisionedV2Peer, V2StartupError, provision_v2_peer

pytestmark = pytest.mark.skipif(os.name != "nt", reason="v2 startup requires native verified NTFS evidence")

MOD = b"public-mod-manifest"
POLICY = b"public-policy-config"
BINDING = b"public-binding-manifest"
PSK = bytes(range(32))
CHALLENGE_ID = "11111111-2222-3333-4444-555555555555"
ISSUED_AT = datetime(2026, 1, 2, 3, 4, 5, 678000, tzinfo=timezone.utc)
EXPIRES_AT = ISSUED_AT + timedelta(seconds=5)
PEER_NONCE = b"p" * 32
HUB_NONCE = b"h" * 32


def _manifest(role="SERVER_AUTHORITY", peer="server-test-1"):
    return DeploymentManifest(
        schema="luosandbox.minecraft-control-deployment.v2",
        security="luosandbox.control-auth.v1",
        peer_role=role,
        peer_install_id=peer,
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


def _provision(root: Path, manifest):
    paths = [root / name for name in ("deployment.json", "mod.json", "policy.json", "binding.json")]
    for path, raw in zip(paths, (manifest.raw_bytes, MOD, POLICY, BINDING)):
        path.write_bytes(raw)
    return DeploymentFileProvision(
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


@pytest.fixture(params=("SERVER_AUTHORITY", "CLIENT_CONTROLLER"))
def provisioned_peer(tmp_path, request):
    root = tmp_path / "trusted"
    root.mkdir()
    manifest = _manifest(request.param, "server-test-1" if request.param == "SERVER_AUTHORITY" else "client-test-1")
    files = _provision(root, manifest)
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
            pytest.skip(f"verified v2 startup requires NTFS, found {filesystem_name.value!r}")
    finally:
        assert kernel.CloseHandle(root_handle)
    for path in (
        files.deployment_manifest_path,
        files.mod_manifest_path,
        files.policy_config_path,
        files.binding_manifest_path,
    ):
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
    peer = provision_v2_peer(
        verified,
        expected_peer_role=manifest.peer_role,
        expected_peer_install_id=manifest.peer_install_id,
        expected_hub_install_id=manifest.hub_install_id,
        expected_config_hash=manifest.config_hash,
        key_id="role-key-1",
        role_psk=PSK,
    )
    try:
        yield peer, verified, files
    finally:
        peer.close()


def _values():
    return PEER_NONCE, HUB_NONCE, CHALLENGE_ID, ISSUED_AT, EXPIRES_AT


def _authenticated(context):
    return AuthenticatedTranscript(
        context=context,
        peer_nonce=PEER_NONCE,
        hub_nonce=HUB_NONCE,
        challenge_id=CHALLENGE_ID,
        issued_at=ISSUED_AT,
        expires_at=EXPIRES_AT,
    )


def test_actual_verified_filesystem_mints_snapshot_bound_context_and_known_proofs(provisioned_peer):
    peer, verified, _files = provisioned_peer
    snapshot = verified.snapshot
    assert peer.is_provisioned
    assert peer.context == ControlAuthContext(
        peer_role=snapshot.peer_role,
        peer_install_id=snapshot.peer_install_id,
        hub_install_id=snapshot.hub_install_id,
        key_id="role-key-1",
        protocol="LUO_CONTROL_POSTAUTH_V2",
        mod_hash=snapshot.mod_manifest_hash,
        config_hash=snapshot.config_hash,
        binding_hash=snapshot.binding_manifest_hash,
    )
    assert peer.compute_hub_proof(*_values()) == compute_proof(PSK, "HUB_PROOF", peer.context, *_values())
    peer_proof = compute_proof(PSK, "PEER_PROOF", peer.context, *_values())
    ready = peer.verify_peer_proof_and_ready(peer_proof, *_values())
    assert ready.proof == compute_proof(PSK, "READY_PROOF", peer.context, *_values())
    assert "<redacted>" in repr(ready) and ready.proof not in repr(ready)


def test_session_keys_match_independent_actual_derivation_without_raw_getter(provisioned_peer):
    peer, _verified, _files = provisioned_peer
    authenticated = _authenticated(peer.context)
    actual = peer.derive_session_keys(authenticated)
    expected = PostAuthSessionKeys(authenticated, PSK)
    try:
        message = b"fixed authenticated frame"
        assert actual.mac("PEER_TO_HUB", message) == expected.mac("PEER_TO_HUB", message)
        assert actual.mac("HUB_TO_PEER", message) == expected.mac("HUB_TO_PEER", message)
        assert not hasattr(peer, "role_psk") and not hasattr(peer, "raw_key")
    finally:
        actual.close()
        expected.close()


def test_wrong_proof_context_nonce_and_time_are_rejected(provisioned_peer):
    peer, _verified, _files = provisioned_peer
    proof = compute_proof(PSK, "PEER_PROOF", peer.context, *_values())
    with pytest.raises(V2StartupError, match="proof rejected"):
        peer.verify_peer_proof_and_ready("A" * 43, *_values())
    altered = (b"x" * 32, HUB_NONCE, CHALLENGE_ID, ISSUED_AT, EXPIRES_AT)
    with pytest.raises(V2StartupError, match="proof rejected"):
        peer.verify_peer_proof_and_ready(proof, *altered)
    for values in (
        (b"\0" * 32, HUB_NONCE, CHALLENGE_ID, ISSUED_AT, EXPIRES_AT),
        (PEER_NONCE, HUB_NONCE, "00000000-0000-0000-0000-000000000000", ISSUED_AT, EXPIRES_AT),
        (PEER_NONCE, HUB_NONCE, CHALLENGE_ID, ISSUED_AT, EXPIRES_AT + timedelta(milliseconds=1)),
        (PEER_NONCE, HUB_NONCE, CHALLENGE_ID, ISSUED_AT.replace(microsecond=678001), EXPIRES_AT),
    ):
        with pytest.raises(V2StartupError):
            peer.compute_hub_proof(*values)
    wrong_context = ControlAuthContext(
        peer_role=peer.context.peer_role,
        peer_install_id=peer.context.peer_install_id,
        hub_install_id=peer.context.hub_install_id,
        key_id="other-key",
        protocol=peer.context.protocol,
        mod_hash=peer.context.mod_hash,
        config_hash=peer.context.config_hash,
        binding_hash=peer.context.binding_hash,
    )
    with pytest.raises(V2StartupError):
        peer.derive_session_keys(_authenticated(wrong_context))


def test_public_fake_unverified_copy_replace_and_pickle_are_rejected(provisioned_peer):
    peer, _verified, files = provisioned_peer
    with pytest.raises(V2StartupError):
        ProvisionedV2Peer()
    with pytest.raises(V2StartupError):
        copy.copy(peer)
    with pytest.raises(V2StartupError):
        copy.deepcopy(peer)
    with pytest.raises((TypeError, V2StartupError)):
        replace(peer)
    with pytest.raises(V2StartupError):
        pickle.dumps(peer)
    with pytest.raises(V2StartupError):
        copy.copy(peer._owner)
    with pytest.raises(V2StartupError):
        copy.deepcopy(peer._owner)
    with pytest.raises(V2StartupError):
        pickle.dumps(peer._owner)
    unverified = load_unverified_deployment_snapshot(files)
    with pytest.raises(V2StartupError):
        _provision_from(unverified)
    fake = object.__new__(VerifiedDeploymentFilesystem)
    object.__setattr__(fake, "snapshot", unverified)
    object.__setattr__(fake, "root_volume_serial", 1)
    object.__setattr__(fake, "root_file_index", 1)
    object.__setattr__(fake, "_marker", object())
    with pytest.raises(V2StartupError):
        _provision_from(fake)


def test_expected_identity_hash_and_key_inputs_are_mandatory(provisioned_peer):
    peer, verified, _files = provisioned_peer
    snapshot = verified.snapshot
    valid = dict(
        filesystem=verified,
        expected_peer_role=snapshot.peer_role,
        expected_peer_install_id=snapshot.peer_install_id,
        expected_hub_install_id=snapshot.hub_install_id,
        expected_config_hash=snapshot.config_hash,
        key_id="role-key-2",
        role_psk=PSK,
    )
    mutations = (
        {"expected_peer_role": "AGENT_HUB"},
        {"expected_peer_role": 1},
        {"expected_peer_install_id": "wrong-peer"},
        {"expected_hub_install_id": "wrong-hub"},
        {"expected_config_hash": b"z" * 32},
        {"key_id": "bad key"},
        {"role_psk": b"x" * 31},
        {"role_psk": bytearray(PSK)},
        {"role_psk": memoryview(PSK)},
    )
    for mutation in mutations:
        with pytest.raises(V2StartupError):
            provision_v2_peer(**(valid | mutation))
    peer.close()


def test_close_zeroes_owned_clone_and_rejects_all_crypto(provisioned_peer):
    peer, _verified, _files = provisioned_peer
    retained = peer._owner._key
    assert bytes(retained) == PSK and "PSK" not in repr(peer)
    peer.close()
    peer.close()
    assert not peer.is_provisioned and bytes(retained) == b"\0" * 32
    with pytest.raises(V2StartupError):
        peer.compute_hub_proof(*_values())
    with pytest.raises(V2StartupError):
        peer.verify_peer_proof_and_ready("A" * 43, *_values())
    with pytest.raises(V2StartupError):
        peer.derive_session_keys(_authenticated(peer.context))


def test_context_manager_closes_on_ordinary_and_fatal_body_exception(provisioned_peer):
    peer, verified, _files = provisioned_peer
    with pytest.raises(RuntimeError):
        with peer:
            raise RuntimeError("body failure")
    assert not peer.is_provisioned
    replacement = _provision_from(verified)
    with pytest.raises(KeyboardInterrupt):
        with replacement:
            raise KeyboardInterrupt
    assert not replacement.is_provisioned


def test_existing_pending_v2_flow_mints_full_handshake_and_derives_matching_keys(provisioned_peer):
    peer, _verified, _files = provisioned_peer
    pending = PendingHandshakes()
    connection_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    item, hub_proof = pending.begin_v2(
        connection_id,
        peer,
        PEER_NONCE,
        ISSUED_AT,
        hub_nonce=HUB_NONCE,
        challenge_id=CHALLENGE_ID,
        now=0,
        utc_now=ISSUED_AT,
    )
    assert hub_proof == compute_proof(PSK, "HUB_PROOF", peer.context, *_values())
    peer_proof = compute_proof(PSK, "PEER_PROOF", peer.context, *_values())
    authenticated = pending.consume_v2(
        connection_id,
        CHALLENGE_ID,
        peer_proof,
        peer,
        expected_context=peer.context,
        now=1,
        utc_now=ISSUED_AT,
    )
    assert authenticated.has_full_provenance
    assert authenticated.connection_id == connection_id
    assert authenticated.context is peer.context
    assert authenticated.peer_nonce == PEER_NONCE and authenticated.hub_nonce == HUB_NONCE
    assert authenticated.issued_at == ISSUED_AT and authenticated.expires_at == EXPIRES_AT
    assert authenticated.ready_proof == compute_proof(PSK, "READY_PROOF", peer.context, *_values())
    transcript_value = AuthenticatedTranscript(
        context=authenticated.context,
        peer_nonce=authenticated.peer_nonce,
        hub_nonce=authenticated.hub_nonce,
        challenge_id=authenticated.challenge_id,
        issued_at=authenticated.issued_at,
        expires_at=authenticated.expires_at,
    )
    actual = peer.derive_session_keys(transcript_value)
    expected = PostAuthSessionKeys(transcript_value, PSK)
    try:
        message = b"pending-consumed-v2-frame"
        assert actual.mac("PEER_TO_HUB", message) == expected.mac("PEER_TO_HUB", message)
        assert actual.mac("HUB_TO_PEER", message) == expected.mac("HUB_TO_PEER", message)
    finally:
        actual.close()
        expected.close()
    assert item._v2_credential is peer
    assert not hasattr(item, "role_psk") and PSK.hex() not in repr(item)
    with pytest.raises(ControlAuthError):
        pending.consume_v2(
            connection_id,
            CHALLENGE_ID,
            peer_proof,
            peer,
            expected_context=peer.context,
            now=1,
            utc_now=ISSUED_AT,
        )


def test_v2_wrong_proof_credential_and_context_consume_only_correct_connection(provisioned_peer):
    peer, verified, _files = provisioned_peer
    other = _provision_from(verified, key_id="role-key-1")
    connection_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    other_connection = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeef"
    try:
        pending = PendingHandshakes()
        pending.begin_v2(
            connection_id,
            peer,
            PEER_NONCE,
            ISSUED_AT,
            hub_nonce=HUB_NONCE,
            challenge_id=CHALLENGE_ID,
            now=0,
            utc_now=ISSUED_AT,
        )
        with pytest.raises(ControlAuthError):
            pending.consume_v2(
                other_connection,
                CHALLENGE_ID,
                "A" * 43,
                peer,
                expected_context=peer.context,
                now=1,
                utc_now=ISSUED_AT,
            )
        proof = compute_proof(PSK, "PEER_PROOF", peer.context, *_values())
        assert pending.consume_v2(
            connection_id,
            CHALLENGE_ID,
            proof,
            peer,
            expected_context=peer.context,
            now=1,
            utc_now=ISSUED_AT,
        ).has_full_provenance

        equal_but_distinct_context = ControlAuthContext(
            peer_role=peer.context.peer_role,
            peer_install_id=peer.context.peer_install_id,
            hub_install_id=peer.context.hub_install_id,
            key_id=peer.context.key_id,
            protocol=peer.context.protocol,
            mod_hash=peer.context.mod_hash,
            config_hash=peer.context.config_hash,
            binding_hash=peer.context.binding_hash,
        )
        assert equal_but_distinct_context == peer.context and equal_but_distinct_context is not peer.context
        for failure in ("proof", "credential", "context"):
            pending.begin_v2(
                connection_id,
                peer,
                PEER_NONCE,
                ISSUED_AT,
                hub_nonce=HUB_NONCE,
                challenge_id=CHALLENGE_ID,
                now=2,
                utc_now=ISSUED_AT,
            )
            with pytest.raises(ControlAuthError):
                pending.consume_v2(
                    connection_id,
                    CHALLENGE_ID,
                    "A" * 43 if failure == "proof" else proof,
                    other if failure == "credential" else peer,
                    expected_context=equal_but_distinct_context if failure == "context" else peer.context,
                    now=3,
                    utc_now=ISSUED_AT,
                )
            with pytest.raises(ControlAuthError):
                pending.consume_v2(
                    connection_id,
                    CHALLENGE_ID,
                    proof,
                    peer,
                    expected_context=peer.context,
                    now=3,
                    utc_now=ISSUED_AT,
                )
    finally:
        other.close()


def test_closed_v2_credential_rejects_begin_and_consume(provisioned_peer):
    peer, verified, _files = provisioned_peer
    connection_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    peer.close()
    with pytest.raises(ControlAuthError):
        PendingHandshakes().begin_v2(
            connection_id,
            peer,
            PEER_NONCE,
            ISSUED_AT,
            hub_nonce=HUB_NONCE,
            challenge_id=CHALLENGE_ID,
            now=0,
            utc_now=ISSUED_AT,
        )
    replacement = _provision_from(verified)
    pending = PendingHandshakes()
    try:
        pending.begin_v2(
            connection_id,
            replacement,
            PEER_NONCE,
            ISSUED_AT,
            hub_nonce=HUB_NONCE,
            challenge_id=CHALLENGE_ID,
            now=0,
            utc_now=ISSUED_AT,
        )
        proof = compute_proof(PSK, "PEER_PROOF", replacement.context, *_values())
        replacement.close()
        with pytest.raises(ControlAuthError):
            pending.consume_v2(
                connection_id,
                CHALLENGE_ID,
                proof,
                replacement,
                expected_context=replacement.context,
                now=1,
                utc_now=ISSUED_AT,
            )
    finally:
        replacement.close()


def _provision_from(filesystem, *, key_id="role-key-test"):
    snapshot = filesystem.snapshot if isinstance(filesystem, VerifiedDeploymentFilesystem) else filesystem
    if isinstance(snapshot, DeploymentFileSnapshot):
        role = snapshot.peer_role
        peer_id = snapshot.peer_install_id
        hub_id = snapshot.hub_install_id
        config_hash = snapshot.config_hash
    else:
        role = "SERVER_AUTHORITY"
        peer_id = "server-test-1"
        hub_id = "hub-test-1"
        config_hash = b"x" * 32
    return provision_v2_peer(
        filesystem,
        expected_peer_role=role,
        expected_peer_install_id=peer_id,
        expected_hub_install_id=hub_id,
        expected_config_hash=config_hash,
        key_id=key_id,
        role_psk=PSK,
    )
