import ctypes
import os
from dataclasses import FrozenInstanceError
from hashlib import sha256
from pathlib import Path

import pytest

from src.adapter.minecraft.deployment_files import (
    DeploymentFileError,
    DeploymentFileProvision,
    _WindowsReadBackend,
    load_unverified_deployment_snapshot,
)
from src.adapter.minecraft.deployment_manifest import (
    DeploymentManifest,
    TrustedDeploymentIdentity,
)

MOD = b"public-mod-manifest"
POLICY = b"public-policy-config"
BINDING = b"public-binding-manifest"


def manifest(role="SERVER_AUTHORITY", peer="server-test-1"):
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


def provision(root: Path, value=None):
    value = value or manifest()
    paths = [root / name for name in ("deployment.json", "mod.json", "policy.json", "binding.json")]
    for path, raw in zip(paths, (value.raw_bytes, MOD, POLICY, BINDING)):
        path.write_bytes(raw)
    return DeploymentFileProvision(
        deployment_manifest_path=paths[0],
        mod_manifest_path=paths[1],
        policy_config_path=paths[2],
        binding_manifest_path=paths[3],
        trusted_identity=TrustedDeploymentIdentity(
            peer_role=value.peer_role,
            peer_install_id=value.peer_install_id,
            hub_install_id=value.hub_install_id,
            expected_config_hash=value.config_hash,
        ),
    )


@pytest.mark.skipif(os.name != "nt", reason="requires actual Win32 file handles")
@pytest.mark.parametrize(("role", "peer"), (("SERVER_AUTHORITY", "server-1"), ("CLIENT_CONTROLLER", "client-1")))
def test_real_windows_reads_hashes_both_roles_and_remains_nonauthorizing(tmp_path, role, peer):
    value = manifest(role, peer)
    result = load_unverified_deployment_snapshot(provision(tmp_path, value))
    assert result.peer_role == role and result.peer_install_id == peer
    assert result.config_hash == value.config_hash
    assert result.mod_manifest_hash == sha256(MOD).digest()
    assert result.filesystem_status == "FILES_READ_AND_HASHED"
    assert result.security_status == "ACL_AND_PARENT_NAMESPACE_UNVERIFIED"
    assert not result.parent_namespace_anchoring_verified
    assert not result.acl_verified
    assert not result.activation_allowed
    assert str(tmp_path) not in repr(result)
    with pytest.raises((AttributeError, FrozenInstanceError)):
        result.activation_allowed = True


@pytest.mark.parametrize(
    "path",
    (
        Path("relative.json"),
        Path("C:relative.json"),
        Path(r"\\server\share\file"),
        Path(r"\\?\C:\file"),
        Path(r"\\.\C:\file"),
        Path(r"C:\file:stream"),
        Path(r"C:\bad*name"),
        Path("C:\\bad\0name"),
    ),
)
def test_paths_are_explicit_conservative_windows_absolute_files(path):
    identity = TrustedDeploymentIdentity(
        peer_role="SERVER_AUTHORITY",
        peer_install_id="server",
        hub_install_id="hub",
        expected_config_hash=b"x" * 32,
    )
    with pytest.raises(DeploymentFileError, match="invalid deployment file path"):
        DeploymentFileProvision(
            deployment_manifest_path=path,
            mod_manifest_path=Path(r"C:\mod"),
            policy_config_path=Path(r"C:\policy"),
            binding_manifest_path=Path(r"C:\binding"),
            trusted_identity=identity,
        )


class Handle:
    def __init__(self, raw=b"x", close_error=None):
        self.raw = raw
        self.close_error = close_error
        self.closes = 0

    def close(self):
        self.closes += 1
        if self.close_error is not None:
            raise self.close_error


class Backend:
    def __init__(self, raws, fail_open=None, close_errors=None, mutate=False):
        self.raws = raws
        self.fail_open = fail_open
        self.close_errors = close_errors or {}
        self.mutate = mutate
        self.handles = []
        self.opens = 0
        self.all_open_seen = False

    def screen_components(self, _path):
        pass

    def open(self, _path):
        index = self.opens
        self.opens += 1
        if self.fail_open == index:
            raise DeploymentFileError("injected open failure")
        handle = Handle(self.raws[index], self.close_errors.get(index))
        self.handles.append(handle)
        return handle

    def after_all_open(self, handles):
        self.all_open_seen = len(handles) == 4

    def read_stable(self, handle, limit):
        if self.mutate:
            raise DeploymentFileError("injected identity mismatch")
        if not handle.raw or len(handle.raw) > limit:
            raise DeploymentFileError("injected size failure")
        return bytes(handle.raw)


def fake_provision(value=None, identity=None):
    value = value or manifest()
    return DeploymentFileProvision(
        deployment_manifest_path=Path(r"C:\deployment.json"),
        mod_manifest_path=Path(r"C:\mod.json"),
        policy_config_path=Path(r"C:\policy.json"),
        binding_manifest_path=Path(r"C:\binding.json"),
        trusted_identity=identity
        or TrustedDeploymentIdentity(
            peer_role=value.peer_role,
            peer_install_id=value.peer_install_id,
            hub_install_id=value.hub_install_id,
            expected_config_hash=value.config_hash,
        ),
    )


def raws(value=None):
    value = value or manifest()
    return [value.raw_bytes, MOD, POLICY, BINDING]


def test_fake_backend_opens_all_four_before_read_and_closes_every_handle():
    backend = Backend(raws())
    result = load_unverified_deployment_snapshot(fake_provision(), _backend=backend)
    assert result.config_hash == manifest().config_hash
    assert backend.all_open_seen and [handle.closes for handle in backend.handles] == [1, 1, 1, 1]


@pytest.mark.parametrize("index", range(4))
def test_partial_open_failure_closes_every_prior_handle_once(index):
    backend = Backend(raws(), fail_open=index)
    with pytest.raises(DeploymentFileError):
        load_unverified_deployment_snapshot(fake_provision(), _backend=backend)
    assert all(handle.closes == 1 for handle in backend.handles)


def test_size_identity_and_artifact_or_trusted_identity_mismatch_fail_without_snapshot():
    for backend in (Backend([b"", MOD, POLICY, BINDING]), Backend(raws(), mutate=True)):
        with pytest.raises(DeploymentFileError):
            load_unverified_deployment_snapshot(fake_provision(), _backend=backend)
    changed = raws()
    changed[2] += b"x"
    with pytest.raises(DeploymentFileError):
        load_unverified_deployment_snapshot(fake_provision(), _backend=Backend(changed))
    identity = TrustedDeploymentIdentity(
        peer_role="CLIENT_CONTROLLER",
        peer_install_id="server-test-1",
        hub_install_id="hub-test-1",
        expected_config_hash=manifest().config_hash,
    )
    with pytest.raises(DeploymentFileError):
        load_unverified_deployment_snapshot(fake_provision(identity=identity), _backend=Backend(raws()))


def test_cleanup_failure_and_fatal_error_close_all_handles_once_and_never_return_snapshot():
    first = RuntimeError("ordinary")
    fatal = KeyboardInterrupt()
    backend = Backend(raws(), close_errors={3: first, 1: fatal})
    with pytest.raises(KeyboardInterrupt):
        load_unverified_deployment_snapshot(fake_provision(), _backend=backend)
    assert [handle.closes for handle in backend.handles] == [1, 1, 1, 1]


def test_errors_are_sanitized_without_paths_or_backend_text():
    backend = Backend(raws(), fail_open=2)
    with pytest.raises(DeploymentFileError) as error:
        load_unverified_deployment_snapshot(fake_provision(), _backend=backend)
    assert "C:\\" not in str(error.value)
    assert "injected" not in str(error.value)


@pytest.mark.skipif(os.name != "nt", reason="requires actual Win32 sharing semantics")
def test_real_handles_block_writer_while_all_open_and_release_after_return(tmp_path):
    value = manifest()
    item = provision(tmp_path, value)
    observed = []

    def after_open(_handles):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel.CreateFileW(
            str(item.policy_config_path),
            0x40000000,
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            0x80,
            None,
        )
        observed.append(handle in (None, ctypes.c_void_p(-1).value))
        if handle not in (None, ctypes.c_void_p(-1).value):
            kernel.CloseHandle(handle)

    backend = _WindowsReadBackend(after_open)
    load_unverified_deployment_snapshot(item, _backend=backend)
    assert observed == [True]
    with item.policy_config_path.open("ab") as output:
        output.write(b"")
