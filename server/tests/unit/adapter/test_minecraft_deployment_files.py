import copy
import ctypes
import os
import struct
import subprocess
from ctypes import wintypes
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from pathlib import Path

import pytest

from src.adapter.minecraft.deployment_acl import TrustedDeploymentAclPolicy, inspect_handle_security_descriptor
from src.adapter.minecraft.deployment_files import (
    DeploymentFileError,
    DeploymentFileProvision,
    TrustedDeploymentRoot,
    VerifiedDeploymentFilesystem,
    _file_info,
    _FileIdentity,
    _VolumeFacts,
    _WindowsNamespaceBackend,
    _WindowsReadBackend,
    load_unverified_deployment_snapshot,
    load_verified_deployment_filesystem,
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


@pytest.mark.skipif(os.name != "nt", reason="requires actual Win32 namespace handles")
def test_verified_loader_holds_ancestors_children_and_acl_until_success(tmp_path, monkeypatch):
    root = tmp_path / "trusted"
    root.mkdir()
    item = provision(root)
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
    root_handle = kernel.CreateFileW(
        str(root), 0x00020000 | 0x00040000 | 0x80, 1, None, 3, 0x02000000 | 0x00200000, None
    )
    assert root_handle not in (None, ctypes.c_void_p(-1).value)
    try:
        identity = _file_info(kernel, root_handle)
        descriptor = inspect_handle_security_descriptor(root_handle)
    finally:
        assert kernel.CloseHandle(root_handle)
    for path in (
        item.deployment_manifest_path,
        item.mod_manifest_path,
        item.policy_config_path,
        item.binding_manifest_path,
    ):
        handle = kernel.CreateFileW(
            str(path), 0x00020000 | 0x00040000, 0x1 | 0x2 | 0x4, None, 3, 0x80 | 0x00200000, None
        )
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
    trusted = TrustedDeploymentRoot(
        root_path=root,
        expected_volume_serial=identity.volume_serial,
        expected_file_index=identity.file_index,
        acl_policy=TrustedDeploymentAclPolicy(
            expected_owner_sid=descriptor.owner_sid,
            allowed_writer_sids=(descriptor.owner_sid,),
        ),
    )
    observed = []
    volume_facts = []
    original_volume_facts = _WindowsNamespaceBackend.volume_facts

    def record_volume_facts(self, handle):
        value = original_volume_facts(self, handle)
        volume_facts.append(value)
        return value

    monkeypatch.setattr(_WindowsNamespaceBackend, "volume_facts", record_volume_facts)

    def after_open(ancestors, children):
        assert volume_facts and volume_facts[0].filesystem_name == "NTFS"
        observed.append((len(ancestors), len(children)))

    backend = _WindowsNamespaceBackend(after_open)
    result = load_verified_deployment_filesystem(item, trusted, _backend=backend)
    assert result.filesystem_verified and observed and observed[0][1] == 4
    assert len(volume_facts) == 2 and volume_facts[0] == volume_facts[1]
    assert not result.snapshot.activation_allowed
    with pytest.raises(DeploymentFileError):
        VerifiedDeploymentFilesystem()
    with pytest.raises(DeploymentFileError):
        copy.copy(result)
    with pytest.raises(DeploymentFileError):
        copy.deepcopy(result)
    with pytest.raises((TypeError, DeploymentFileError)):
        replace(result)


def test_verified_loader_rejects_fake_backend_and_non_direct_children(tmp_path):
    identity = TrustedDeploymentIdentity(
        peer_role="SERVER_AUTHORITY", peer_install_id="server", hub_install_id="hub", expected_config_hash=b"x" * 32
    )
    provisioned = DeploymentFileProvision(
        deployment_manifest_path=Path(r"C:\root\deployment"),
        mod_manifest_path=Path(r"C:\root\mod"),
        policy_config_path=Path(r"C:\root\policy"),
        binding_manifest_path=Path(r"C:\other\binding"),
        trusted_identity=identity,
    )
    policy = TrustedDeploymentAclPolicy(
        expected_owner_sid=b"\x01\x01" + (5).to_bytes(6, "big") + (1).to_bytes(4, "little"),
        allowed_writer_sids=(b"\x01\x01" + (5).to_bytes(6, "big") + (1).to_bytes(4, "little"),),
    )
    root = TrustedDeploymentRoot(
        root_path=Path(r"C:\root"), expected_volume_serial=1, expected_file_index=1, acl_policy=policy
    )
    with pytest.raises(DeploymentFileError):
        load_verified_deployment_filesystem(provisioned, root, _backend=Backend(raws()))
    with pytest.raises(DeploymentFileError, match="direct trusted-root children"):
        load_verified_deployment_filesystem(provisioned, root, _backend=_WindowsNamespaceBackend())


@pytest.mark.parametrize(
    ("serial", "index"),
    ((0, 1), (1, 0), (True, 1), (1, True)),
)
def test_trusted_root_rejects_zero_and_bool_identity_values(serial, index):
    policy = TrustedDeploymentAclPolicy(
        expected_owner_sid=b"\x01\x01" + (5).to_bytes(6, "big") + (1).to_bytes(4, "little"),
        allowed_writer_sids=(b"\x01\x01" + (5).to_bytes(6, "big") + (1).to_bytes(4, "little"),),
    )
    with pytest.raises(DeploymentFileError):
        TrustedDeploymentRoot(
            root_path=Path(r"C:\root"),
            expected_volume_serial=serial,
            expected_file_index=index,
            acl_policy=policy,
        )


def test_native_backend_semantic_failures_never_mint_and_close_all_owned_handles(monkeypatch):
    import src.adapter.minecraft.deployment_files as module

    sid_value = b"\x01\x01" + (5).to_bytes(6, "big") + (1).to_bytes(4, "little")
    policy = TrustedDeploymentAclPolicy(expected_owner_sid=sid_value, allowed_writer_sids=(sid_value,))
    trusted = TrustedDeploymentRoot(
        root_path=Path(r"C:\root"), expected_volume_serial=7, expected_file_index=30, acl_policy=policy
    )
    provisioned = DeploymentFileProvision(
        deployment_manifest_path=Path(r"C:\root\deployment"),
        mod_manifest_path=Path(r"C:\root\mod"),
        policy_config_path=Path(r"C:\root\policy"),
        binding_manifest_path=Path(r"C:\root\binding"),
        trusted_identity=TrustedDeploymentIdentity(
            peer_role="SERVER_AUTHORITY",
            peer_install_id="server-test-1",
            hub_install_id="hub-test-1",
            expected_config_hash=manifest().config_hash,
        ),
    )
    descriptor = type("Descriptor", (), {})()
    assessment = type("Assessment", (), {"accepted": True})()
    monkeypatch.setattr(module, "inspect_and_assess_handle", lambda *_: (descriptor, assessment))

    for mode in (
        "wrong-volume",
        "zero-index",
        "changed-ancestor",
        "no-persistent-acl",
        "refs",
        "unknown-filesystem",
        "empty-filesystem",
        "volume-query-failure",
        "filesystem-changed",
    ):
        backend = _WindowsNamespaceBackend()
        opened = []
        counter = [0]
        backend.drive_mapping = lambda _root: "\\device\\harddiskvolume1"
        backend.open_directory = lambda _path: opened.append(Handle()) or opened[-1]
        backend.open_file = lambda _path: opened.append(Handle()) or opened[-1]

        def identity(handle, *, directory):
            counter[0] += 1
            serial = 8 if mode == "wrong-volume" and counter[0] == 2 else 7
            index = 0 if mode == "zero-index" and counter[0] == 2 else (30 if counter[0] == 3 else counter[0] + 10)
            if mode == "changed-ancestor" and counter[0] > 7:
                index += 100
            return _FileIdentity(0x10 if directory else 0, serial, index, 10, 1)

        backend.identity = identity
        volume_queries = [0]

        def volume_facts(_handle):
            volume_queries[0] += 1
            if mode == "volume-query-failure":
                raise DeploymentFileError("injected volume query failure")
            filesystem_name = {
                "refs": "REFS",
                "unknown-filesystem": "UNKNOWN",
                "empty-filesystem": "",
            }.get(mode, "NTFS")
            if mode == "filesystem-changed" and volume_queries[0] > 1:
                filesystem_name = "REFS"
            return _VolumeFacts(7, mode != "no-persistent-acl", filesystem_name)

        backend.volume_facts = volume_facts
        backend.after_all_open = lambda *_: None
        backend.read_stable = lambda _handle, _limit: raws()[len(opened) - 7]
        with pytest.raises(DeploymentFileError):
            load_verified_deployment_filesystem(provisioned, trusted, _backend=backend)
        assert opened and all(handle.closes == 1 for handle in opened)


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


def _native_kernel():
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
    kernel.MoveFileExW.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint)
    kernel.MoveFileExW.restype = ctypes.c_int
    kernel.SetFileInformationByHandle.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint,
    )
    kernel.SetFileInformationByHandle.restype = ctypes.c_int
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
    return kernel


class _FILE_RENAME_CHOICE(ctypes.Union):
    _fields_ = (("ReplaceIfExists", wintypes.BOOLEAN), ("Flags", wintypes.DWORD))


class _FILE_RENAME_INFO(ctypes.Structure):
    _anonymous_ = ("choice",)
    _fields_ = (
        ("choice", _FILE_RENAME_CHOICE),
        ("RootDirectory", wintypes.HANDLE),
        ("FileNameLength", wintypes.DWORD),
        ("FileName", wintypes.WCHAR * 1),
    )


def _rename_by_handle(kernel, handle, destination):
    expected_offsets = (8, 16, 20) if ctypes.sizeof(ctypes.c_void_p) == 8 else (4, 8, 12)
    actual_offsets = (
        _FILE_RENAME_INFO.RootDirectory.offset,
        _FILE_RENAME_INFO.FileNameLength.offset,
        _FILE_RENAME_INFO.FileName.offset,
    )
    assert actual_offsets == expected_offsets
    encoded = str(destination).encode("utf-16-le")
    buffer_size = _FILE_RENAME_INFO.FileName.offset + len(encoded)

    class _AlignedRenameBuffer(ctypes.Union):
        _fields_ = (("alignment", ctypes.c_void_p), ("raw", ctypes.c_ubyte * buffer_size))

    buffer = _AlignedRenameBuffer()
    assert ctypes.alignment(buffer) >= ctypes.alignment(_FILE_RENAME_INFO)
    value = ctypes.cast(ctypes.byref(buffer), ctypes.POINTER(_FILE_RENAME_INFO)).contents
    value.ReplaceIfExists = False
    value.RootDirectory = None
    value.FileNameLength = len(encoded)
    assert _FILE_RENAME_INFO.FileName.offset + len(encoded) <= ctypes.sizeof(buffer)
    ctypes.memmove(ctypes.addressof(buffer) + _FILE_RENAME_INFO.FileName.offset, encoded, len(encoded))
    ctypes.set_last_error(0)
    success = bool(kernel.SetFileInformationByHandle(handle, 3, ctypes.byref(buffer), buffer_size))
    error = 0 if success else ctypes.get_last_error()
    return success, error, actual_offsets


def _filesystem_name(kernel, handle):
    name = ctypes.create_unicode_buffer(64)
    assert kernel.GetVolumeInformationByHandleW(handle, None, 0, None, None, None, name, len(name))
    return name.value


def _open_directory_for_rename(kernel, path):
    ctypes.set_last_error(0)
    handle = kernel.CreateFileW(
        str(path),
        0x00010000 | 0x80,
        1 | 2 | 4,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    error = 0 if handle not in (None, ctypes.c_void_p(-1).value) else ctypes.get_last_error()
    return handle, error


def _prepare_verified_tree(tmp_path):
    held_parent = tmp_path / "held-parent"
    root = held_parent / "trusted"
    root.mkdir(parents=True)
    item = provision(root)
    kernel = _native_kernel()
    root_handle = kernel.CreateFileW(
        str(root), 0x00020000 | 0x00040000 | 0x80, 1, None, 3, 0x02000000 | 0x00200000, None
    )
    assert root_handle not in (None, ctypes.c_void_p(-1).value)
    try:
        identity = _file_info(kernel, root_handle)
        descriptor = inspect_handle_security_descriptor(root_handle)
    finally:
        assert kernel.CloseHandle(root_handle)
    for path in (
        item.deployment_manifest_path,
        item.mod_manifest_path,
        item.policy_config_path,
        item.binding_manifest_path,
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
    trusted = TrustedDeploymentRoot(
        root_path=root,
        expected_volume_serial=identity.volume_serial,
        expected_file_index=identity.file_index,
        acl_policy=TrustedDeploymentAclPolicy(
            expected_owner_sid=descriptor.owner_sid, allowed_writer_sids=(descriptor.owner_sid,)
        ),
    )
    parent_handle = kernel.CreateFileW(
        str(held_parent), 0x00020000 | 0x00040000 | 0x80, 1, None, 3, 0x02000000 | 0x00200000, None
    )
    assert parent_handle not in (None, ctypes.c_void_p(-1).value)
    try:
        _protect_owner_only(parent_handle, descriptor.owner_sid)
    finally:
        assert kernel.CloseHandle(parent_handle)
    return kernel, held_parent, root, item, trusted


@pytest.mark.skipif(os.name != "nt", reason="requires actual Win32 sharing semantics")
def test_native_held_chain_blocks_namespace_and_child_mutations_then_releases(tmp_path):
    kernel, held_parent, root, item, trusted = _prepare_verified_tree(tmp_path)
    parent_renamed = held_parent.with_name("held-parent-renamed")
    root_renamed = root.with_name("trusted-renamed")
    child_renamed = item.policy_config_path.with_name("policy-renamed.json")
    errors = []

    def reject(operation, *, access_denied_ok=False):
        ctypes.set_last_error(0)
        success = bool(operation())
        error = ctypes.get_last_error()
        errors.append(error)
        assert not success and error in ((5, 32) if access_denied_ok else (32,))

    def after_open(_ancestors, _children):
        reject(lambda: kernel.MoveFileExW(str(held_parent), str(parent_renamed), 0), access_denied_ok=True)
        reject(lambda: kernel.MoveFileExW(str(root), str(root_renamed), 0), access_denied_ok=True)

        reject(
            lambda: kernel.MoveFileExW(str(item.policy_config_path), str(child_renamed), 0),
            access_denied_ok=True,
        )
        for access in (0x40000000, 0x00010000):

            def conflict(access=access):
                handle = kernel.CreateFileW(str(item.policy_config_path), access, 7, None, 3, 0x80, None)
                if handle not in (None, ctypes.c_void_p(-1).value):
                    try:
                        return 1
                    finally:
                        kernel.CloseHandle(handle)
                return 0

            reject(conflict)

    result = load_verified_deployment_filesystem(item, trusted, _backend=_WindowsNamespaceBackend(after_open))
    assert result.filesystem_verified and len(errors) == 5
    assert kernel.MoveFileExW(str(held_parent), str(parent_renamed), 0)
    assert kernel.MoveFileExW(str(parent_renamed), str(held_parent), 0)
    assert kernel.MoveFileExW(str(root), str(root_renamed), 0)
    assert kernel.MoveFileExW(str(root_renamed), str(root), 0)
    assert kernel.MoveFileExW(str(item.policy_config_path), str(child_renamed), 0)
    assert kernel.MoveFileExW(str(child_renamed), str(item.policy_config_path), 0)
    for path, access, flags in (
        (item.policy_config_path, 0x40000000, 0x80),
        (item.policy_config_path, 0x00010000, 0x80),
    ):
        handle = kernel.CreateFileW(str(path), access, 7, None, 3, flags, None)
        assert handle not in (None, ctypes.c_void_p(-1).value)
        assert kernel.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="requires actual handle-based Windows rename")
def test_native_root_handle_rename_is_blocked_while_held_then_succeeds_and_restores(tmp_path):
    kernel, _held_parent, root, item, trusted = _prepare_verified_tree(tmp_path)
    renamed = root.with_name("trusted-handle-renamed")
    observed = {}

    def after_open(ancestors, _children):
        loader_root = ancestors[-1].handle
        observed["filesystem"] = _filesystem_name(kernel, loader_root)
        attacker, open_error = _open_directory_for_rename(kernel, root)
        observed["delete_open_error"] = open_error
        if attacker in (None, ctypes.c_void_p(-1).value):
            observed["same_object"] = None
            observed["held_rename"] = None
            assert open_error in (5, 32)
            return
        try:
            loader_identity = _file_info(kernel, loader_root)
            attacker_identity = _file_info(kernel, attacker)
            observed["same_object"] = (
                attacker_identity.volume_serial == loader_identity.volume_serial
                and attacker_identity.file_index == loader_identity.file_index
            )
            success, error, offsets = _rename_by_handle(kernel, attacker, renamed)
            observed["held_rename"] = (success, error)
            observed["offsets"] = offsets
            if success:
                restored, restore_error, _ = _rename_by_handle(kernel, attacker, root)
                observed["unexpected_success_restore"] = (restored, restore_error)
        finally:
            assert kernel.CloseHandle(attacker)

    result = load_verified_deployment_filesystem(item, trusted, _backend=_WindowsNamespaceBackend(after_open))
    assert result.filesystem_verified
    assert (
        observed["filesystem"].upper() == "NTFS"
    ), f"native rename evidence is unsupported on filesystem {observed['filesystem']!r}"
    if observed["delete_open_error"]:
        assert observed["delete_open_error"] in (5, 32)
    else:
        assert observed["same_object"] is True
        assert observed["held_rename"][0] is False
        assert observed["held_rename"][1] != 0
        assert observed["offsets"] == ((8, 16, 20) if ctypes.sizeof(ctypes.c_void_p) == 8 else (4, 8, 12))
        assert "unexpected_success_restore" not in observed

    attacker, open_error = _open_directory_for_rename(kernel, root)
    assert open_error == 0 and attacker not in (None, ctypes.c_void_p(-1).value)
    original_identity = _file_info(kernel, attacker)
    try:
        success, error, offsets = _rename_by_handle(kernel, attacker, renamed)
        assert success and error == 0
        assert not root.exists() and renamed.is_dir()
        renamed_handle, renamed_error = _open_directory_for_rename(kernel, renamed)
        assert renamed_error == 0 and renamed_handle not in (None, ctypes.c_void_p(-1).value)
        try:
            renamed_identity = _file_info(kernel, renamed_handle)
            assert (
                renamed_identity.volume_serial,
                renamed_identity.file_index,
            ) == (original_identity.volume_serial, original_identity.file_index)
        finally:
            assert kernel.CloseHandle(renamed_handle)
        restored, restore_error, restore_offsets = _rename_by_handle(kernel, attacker, root)
        assert restored and restore_error == 0 and restore_offsets == offsets
        assert root.is_dir() and not renamed.exists()
        assert _file_info(kernel, attacker).file_index == original_identity.file_index
    finally:
        if renamed.exists() and not root.exists():
            restored, _restore_error, _ = _rename_by_handle(kernel, attacker, root)
            assert restored
        assert kernel.CloseHandle(attacker)


@pytest.mark.skipif(os.name != "nt", reason="requires mklink /J support")
def test_actual_intermediate_junction_is_rejected_and_target_remains_intact(tmp_path):
    _kernel, target_parent, target_root, item, trusted = _prepare_verified_tree(tmp_path / "target")
    alias = tmp_path / "alias"
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(alias), str(target_parent)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip("host does not permit a local junction")
    try:
        attributes = _native_kernel().GetFileAttributesW(str(alias))
        assert attributes & 0x400
        alias_root = alias / "trusted"
        alias_item = DeploymentFileProvision(
            deployment_manifest_path=alias_root / item.deployment_manifest_path.name,
            mod_manifest_path=alias_root / item.mod_manifest_path.name,
            policy_config_path=alias_root / item.policy_config_path.name,
            binding_manifest_path=alias_root / item.binding_manifest_path.name,
            trusted_identity=item.trusted_identity,
        )
        trusted = TrustedDeploymentRoot(
            root_path=alias_root,
            expected_volume_serial=trusted.expected_volume_serial,
            expected_file_index=trusted.expected_file_index,
            acl_policy=trusted.acl_policy,
        )
        with pytest.raises(DeploymentFileError):
            load_verified_deployment_filesystem(alias_item, trusted)
        assert target_root.is_dir() and item.deployment_manifest_path.is_file()
    finally:
        os.rmdir(alias)
        assert target_root.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="requires actual native handles")
def test_actual_backend_cleanup_unknown_never_mints_and_still_releases_all_handles(tmp_path):
    _kernel, _parent, _root, item, trusted = _prepare_verified_tree(tmp_path)
    closed = []

    def after_open(ancestors, children):
        for handle in (*ancestors, *children):
            original = handle.close

            def counted(original=original, handle=handle):
                original()
                closed.append(handle.state)

            handle.close = counted
        original = children[1].close

        def release_then_fail():
            original()
            raise DeploymentFileError("injected post-release cleanup failure")

        children[1].close = release_then_fail

    with pytest.raises(DeploymentFileError, match="cleanup outcome is unknown"):
        load_verified_deployment_filesystem(item, trusted, _backend=_WindowsNamespaceBackend(after_open))
    assert closed and all(state == "RELEASED" for state in closed)
