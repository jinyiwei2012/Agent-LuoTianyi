"""Windows-only, non-authorizing readers for frozen deployment artifacts.

``load_unverified_deployment_snapshot`` remains the legacy byte/hash-only entry;
its ACL and parent-namespace flags stay false.  The verified entry is an
NTFS-only profile that holds the ancestor chain and child handles while checking
load-time namespace, ACL, identity, and byte consistency.  Its private trusted
identity profile expects the stable 64-bit file identifier exposed by NTFS.
Neither entry authorizes activation, proves durability, or remains a live
filesystem lease after its handles close.
"""

from __future__ import annotations

import ctypes
import os
import re
import threading
from ctypes import wintypes
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PureWindowsPath
from typing import Callable

from .deployment_acl import TrustedDeploymentAclPolicy, inspect_and_assess_handle
from .deployment_manifest import (
    TrustedDeploymentArtifacts,
    TrustedDeploymentIdentity,
    parse_deployment_manifest,
    verify_trusted_deployment,
)

_GENERIC_READ = 0x80000000
_READ_CONTROL = 0x00020000
_FILE_READ_ATTRIBUTES = 0x00000080
_FILE_SHARE_READ = 0x00000001
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_TYPE_DISK = 0x0001
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_INVALID_ATTRIBUTES = 0xFFFFFFFF
_MANIFEST_LIMIT = 4096
_ARTIFACT_LIMIT = 1_048_576
_DRIVE = re.compile(r"[A-Za-z]:\\")
_CANONICAL_DEVICE = re.compile(r"\\Device\\HarddiskVolume[0-9]+", re.IGNORECASE)
_RESERVED_NAME = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", re.IGNORECASE)
_MAX_DIRECTORY_DEPTH = 64
_DRIVE_REMOVABLE = 2
_DRIVE_FIXED = 3
_FILE_SUPPORTS_PERSISTENT_ACLS = 0x00000008
_VERIFIED_FILESYSTEM = object()


class DeploymentFileError(RuntimeError):
    """Sanitized failure with no source path or native error detail."""


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class DeploymentFileProvision:
    deployment_manifest_path: Path
    mod_manifest_path: Path
    policy_config_path: Path
    binding_manifest_path: Path
    trusted_identity: TrustedDeploymentIdentity

    def __post_init__(self) -> None:
        if not isinstance(self.trusted_identity, TrustedDeploymentIdentity):
            raise DeploymentFileError("invalid deployment file provision")
        for name in (
            "deployment_manifest_path",
            "mod_manifest_path",
            "policy_config_path",
            "binding_manifest_path",
        ):
            value = getattr(self, name)
            if type(value) is not Path and not isinstance(value, type(Path("C:\\"))):
                raise DeploymentFileError("invalid deployment file path")
            _validate_windows_path(value)


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class TrustedDeploymentRoot:
    root_path: Path
    expected_volume_serial: int
    expected_file_index: int
    acl_policy: TrustedDeploymentAclPolicy

    def __post_init__(self) -> None:
        _validate_windows_path(self.root_path, directory=True)
        if (
            type(self.expected_volume_serial) is not int
            or not 1 <= self.expected_volume_serial <= 0xFFFFFFFF
            or type(self.expected_file_index) is not int
            or not 1 <= self.expected_file_index <= 0xFFFFFFFFFFFFFFFF
            or not isinstance(self.acl_policy, TrustedDeploymentAclPolicy)
        ):
            raise DeploymentFileError("invalid trusted deployment root")


@dataclass(frozen=True, slots=True, repr=False)
class DeploymentFileSnapshot:
    peer_role: str
    peer_install_id: str
    hub_install_id: str
    config_hash: bytes
    mod_manifest_hash: bytes
    policy_config_hash: bytes
    binding_manifest_hash: bytes
    filesystem_status: str = "FILES_READ_AND_HASHED"
    security_status: str = "ACL_AND_PARENT_NAMESPACE_UNVERIFIED"
    parent_namespace_anchoring_verified: bool = False
    acl_verified: bool = False
    activation_allowed: bool = False

    def __post_init__(self) -> None:
        for name in (
            "config_hash",
            "mod_manifest_hash",
            "policy_config_hash",
            "binding_manifest_hash",
        ):
            value = getattr(self, name)
            if type(value) is not bytes or len(value) != 32:
                raise DeploymentFileError("invalid deployment hash snapshot")
            object.__setattr__(self, name, bytes(value))
        if self.parent_namespace_anchoring_verified or self.acl_verified or self.activation_allowed:
            raise DeploymentFileError("deployment snapshot cannot authorize control")


@dataclass(frozen=True, slots=True, init=False, repr=False)
class VerifiedDeploymentFilesystem:
    snapshot: DeploymentFileSnapshot
    root_volume_serial: int
    root_file_index: int
    _marker: object

    def __init__(self, *args, **kwargs):
        raise DeploymentFileError("verified deployment filesystem is privately minted")

    @classmethod
    def _mint(cls, snapshot: DeploymentFileSnapshot, identity: "_FileIdentity"):
        value = object.__new__(cls)
        object.__setattr__(value, "snapshot", snapshot)
        object.__setattr__(value, "root_volume_serial", identity.volume_serial)
        object.__setattr__(value, "root_file_index", identity.file_index)
        object.__setattr__(value, "_marker", _VERIFIED_FILESYSTEM)
        return value

    @property
    def filesystem_verified(self) -> bool:
        return self._marker is _VERIFIED_FILESYSTEM

    def __copy__(self):
        raise DeploymentFileError("verified deployment filesystem cannot be copied")

    def __deepcopy__(self, memo):
        raise DeploymentFileError("verified deployment filesystem cannot be copied")


def load_unverified_deployment_snapshot(provision: DeploymentFileProvision, *, _backend=None) -> DeploymentFileSnapshot:
    """Read and hash four explicit files; success never authorizes activation."""
    if not isinstance(provision, DeploymentFileProvision):
        raise DeploymentFileError("invalid deployment file provision")
    backend = _backend or _WindowsReadBackend()
    if _backend is None and os.name != "nt":
        raise DeploymentFileError("Windows deployment file reader is unavailable")
    paths = (
        provision.deployment_manifest_path,
        provision.mod_manifest_path,
        provision.policy_config_path,
        provision.binding_manifest_path,
    )
    limits = (_MANIFEST_LIMIT, _ARTIFACT_LIMIT, _ARTIFACT_LIMIT, _ARTIFACT_LIMIT)
    handles: list[_ReadHandle] = []
    primary: BaseException | None = None
    values: list[bytes] | None = None
    try:
        for path in paths:
            backend.screen_components(path)
            handles.append(backend.open(path))
        backend.after_all_open(tuple(handles))
        values = [backend.read_stable(handle, limit) for handle, limit in zip(handles, limits)]
        manifest = parse_deployment_manifest(values[0])
        artifacts = TrustedDeploymentArtifacts(
            mod_manifest_bytes=values[1],
            policy_config_bytes=values[2],
            binding_manifest_bytes=values[3],
        )
        verify_trusted_deployment(manifest, provision.trusted_identity, artifacts)
        snapshot = DeploymentFileSnapshot(
            peer_role=manifest.peer_role,
            peer_install_id=manifest.peer_install_id,
            hub_install_id=manifest.hub_install_id,
            config_hash=manifest.config_hash,
            mod_manifest_hash=sha256(values[1]).digest(),
            policy_config_hash=sha256(values[2]).digest(),
            binding_manifest_hash=sha256(values[3]).digest(),
        )
    except BaseException as error:
        primary = error
        snapshot = None
    cleanup_unknown = False
    for handle in reversed(handles):
        try:
            handle.close()
        except BaseException as error:
            cleanup_unknown = True
            primary = _remember(primary, error)
    if cleanup_unknown:
        _raise_after_cleanup(primary, "deployment file cleanup outcome is unknown")
    if primary is not None:
        _raise_after_cleanup(primary, "deployment file verification failed")
    assert snapshot is not None
    return snapshot


def load_verified_deployment_filesystem(provision, trusted_root, *, _backend=None):  # noqa: C901
    """Hold root ancestry and direct child handles through byte and ACL verification."""
    if not isinstance(provision, DeploymentFileProvision) or not isinstance(trusted_root, TrustedDeploymentRoot):
        raise DeploymentFileError("invalid verified deployment input")
    backend = _backend or _WindowsNamespaceBackend()
    if type(backend) is not _WindowsNamespaceBackend or os.name != "nt":
        raise DeploymentFileError("native Windows deployment verification is required")
    root = trusted_root.root_path
    paths = (
        provision.deployment_manifest_path,
        provision.mod_manifest_path,
        provision.policy_config_path,
        provision.binding_manifest_path,
    )
    _require_direct_children(root, paths)
    ancestors, children = [], []
    primary = None
    minted = None
    try:
        mapping_before = backend.drive_mapping(root)
        ancestor_identities = []
        drive_serial = None
        for path in _ancestor_paths(root):
            handle = backend.open_directory(path)
            ancestors.append(handle)  # Own before validation so failure cleanup includes it.
            identity = backend.identity(handle, directory=True)
            if identity.file_index == 0:
                raise DeploymentFileError("deployment ancestor identity rejected")
            if drive_serial is None:
                drive_serial = identity.volume_serial
            elif identity.volume_serial != drive_serial:
                raise DeploymentFileError("deployment ancestor volume mismatch")
            ancestor_identities.append(identity)
        root_handle = ancestors[-1]
        root_identity = backend.identity(root_handle, directory=True)
        volume_before = backend.volume_facts(root_handle)
        if (
            drive_serial != trusted_root.expected_volume_serial
            or root_identity.volume_serial != trusted_root.expected_volume_serial
            or volume_before.serial != trusted_root.expected_volume_serial
            or volume_before.filesystem_name != "NTFS"
            or root_identity.file_index != trusted_root.expected_file_index
            or not volume_before.persistent_acls
        ):
            raise DeploymentFileError("trusted deployment root identity mismatch")
        root_descriptor, root_assessment = inspect_and_assess_handle(root_handle.handle, trusted_root.acl_policy)
        if not root_assessment.accepted:
            raise DeploymentFileError("trusted deployment root ACL rejected")
        for path in paths:
            children.append(backend.open_file(path))
        backend.after_all_open(tuple(ancestors), tuple(children))
        child_identities = tuple(backend.identity(handle, directory=False) for handle in children)
        if any(
            item.volume_serial != root_identity.volume_serial or item.number_links != 1 or item.file_index == 0
            for item in child_identities
        ):
            raise DeploymentFileError("deployment child identity rejected")
        child_descriptors = []
        for handle in children:
            descriptor, assessment = inspect_and_assess_handle(handle.handle, trusted_root.acl_policy)
            if not assessment.accepted:
                raise DeploymentFileError("deployment child ACL rejected")
            child_descriptors.append(descriptor)
        values = [
            backend.read_stable(handle, limit)
            for handle, limit in zip(children, (_MANIFEST_LIMIT, _ARTIFACT_LIMIT, _ARTIFACT_LIMIT, _ARTIFACT_LIMIT))
        ]
        manifest = parse_deployment_manifest(values[0])
        verify_trusted_deployment(
            manifest,
            provision.trusted_identity,
            TrustedDeploymentArtifacts(
                mod_manifest_bytes=values[1], policy_config_bytes=values[2], binding_manifest_bytes=values[3]
            ),
        )
        if backend.drive_mapping(root) != mapping_before:
            raise DeploymentFileError("deployment drive mapping changed")
        if tuple(backend.identity(handle, directory=True) for handle in ancestors) != tuple(ancestor_identities):
            raise DeploymentFileError("deployment ancestor changed")
        if backend.volume_facts(root_handle) != volume_before:
            raise DeploymentFileError("deployment volume facts changed")
        if backend.identity(root_handle, directory=True) != root_identity:
            raise DeploymentFileError("trusted deployment root changed")
        if tuple(backend.identity(handle, directory=False) for handle in children) != child_identities:
            raise DeploymentFileError("deployment child changed")
        if inspect_and_assess_handle(root_handle.handle, trusted_root.acl_policy)[0] != root_descriptor:
            raise DeploymentFileError("trusted deployment root ACL changed")
        for handle, expected in zip(children, child_descriptors):
            if inspect_and_assess_handle(handle.handle, trusted_root.acl_policy)[0] != expected:
                raise DeploymentFileError("deployment child ACL changed")
        snapshot = DeploymentFileSnapshot(
            peer_role=manifest.peer_role,
            peer_install_id=manifest.peer_install_id,
            hub_install_id=manifest.hub_install_id,
            config_hash=manifest.config_hash,
            mod_manifest_hash=sha256(values[1]).digest(),
            policy_config_hash=sha256(values[2]).digest(),
            binding_manifest_hash=sha256(values[3]).digest(),
        )
        minted = (snapshot, root_identity)
    except BaseException as error:
        primary = error
    cleanup_unknown = False
    for handle in reversed(children):
        try:
            handle.close()
        except BaseException as error:
            cleanup_unknown = True
            primary = _remember(primary, error)
    for handle in reversed(ancestors):
        try:
            handle.close()
        except BaseException as error:
            cleanup_unknown = True
            primary = _remember(primary, error)
    if cleanup_unknown:
        _raise_after_cleanup(primary, "deployment namespace cleanup outcome is unknown")
    if primary is not None:
        _raise_after_cleanup(primary, "deployment filesystem verification failed")
    assert minted is not None
    return VerifiedDeploymentFilesystem._mint(*minted)


def _validate_windows_path(path: Path, *, directory: bool = False) -> None:
    text = str(path)
    if (
        not text
        or "\0" in text
        or any(character in text for character in "*?")
        or text.startswith(("\\\\", "\\?\\", "\\.\\"))
        or _DRIVE.match(text) is None
        or not PureWindowsPath(text).is_absolute()
        or ":" in text[2:]
        or any(part in (".", "..") for part in PureWindowsPath(text).parts)
    ):
        raise DeploymentFileError("invalid deployment file path")
    parts = PureWindowsPath(text).parts[1:]
    if (
        not parts
        or len(parts) > _MAX_DIRECTORY_DEPTH
        or any(
            not part
            or part[-1] in (" ", ".")
            or _RESERVED_NAME.fullmatch(part) is not None
            or any(ord(character) < 32 for character in part)
            for part in parts
        )
    ):
        raise DeploymentFileError("invalid deployment file path")


def _require_direct_children(root: Path, children: tuple[Path, ...]) -> None:
    expected = str(PureWindowsPath(str(root))).casefold()
    if any(str(PureWindowsPath(str(path)).parent).casefold() != expected for path in children):
        raise DeploymentFileError("deployment files must be direct trusted-root children")


def _ancestor_paths(root: Path) -> tuple[Path, ...]:
    pure = PureWindowsPath(str(root))
    current = PureWindowsPath(pure.anchor)
    result = [Path(str(current))]
    for part in pure.parts[1:]:
        current /= part
        result.append(Path(str(current)))
    return tuple(result)


class _ReadHandle:
    def close(self) -> None: ...


class _WindowsReadHandle(_ReadHandle):
    def __init__(self, handle: int):
        self.handle: int | None = handle
        self.state = "ACTIVE"
        self.lock = threading.RLock()

    def close(self) -> None:
        with self.lock:
            if self.state == "RELEASED":
                return
            if self.state != "ACTIVE":
                self.state = "CLEANUP_UNKNOWN"
                raise DeploymentFileError("deployment file cleanup outcome is unknown")
            self.state = "CLOSE_IN_PROGRESS"
            handle = self.handle
            try:
                released = bool(_kernel32().CloseHandle(handle))
            except BaseException as error:
                self.state = "CLEANUP_UNKNOWN"
                if not isinstance(error, Exception):
                    raise
                raise DeploymentFileError("deployment file cleanup outcome is unknown") from error
            if self.state != "CLOSE_IN_PROGRESS" or not released:
                self.state = "CLEANUP_UNKNOWN"
                raise DeploymentFileError("deployment file cleanup outcome is unknown")
            self.handle = None
            self.state = "RELEASED"


class _WindowsReadBackend:
    def __init__(self, after_open: Callable[[tuple[_ReadHandle, ...]], None] | None = None):
        self._after_open = after_open

    def screen_components(self, path: Path) -> None:
        _validate_windows_path(path)
        kernel = _kernel32()
        pure = PureWindowsPath(str(path))
        current = pure.anchor
        for part in pure.parts[1:]:
            current = str(PureWindowsPath(current) / part)
            attributes = kernel.GetFileAttributesW(current)
            if attributes == _INVALID_ATTRIBUTES or attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                raise DeploymentFileError("deployment path screening failed")

    def open(self, path: Path) -> _WindowsReadHandle:
        handle = _kernel32().CreateFileW(
            str(path),
            _GENERIC_READ,
            _FILE_SHARE_READ,
            None,
            _OPEN_EXISTING,
            _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            raise DeploymentFileError("deployment file open failed")
        return _WindowsReadHandle(handle)

    def after_all_open(self, handles: tuple[_ReadHandle, ...]) -> None:
        if self._after_open is not None:
            self._after_open(handles)

    def read_stable(self, handle: _WindowsReadHandle, limit: int) -> bytes:
        if handle.state != "ACTIVE" or handle.handle is None:
            raise DeploymentFileError("deployment file handle unavailable")
        kernel = _kernel32()
        if kernel.GetFileType(handle.handle) != _FILE_TYPE_DISK:
            raise DeploymentFileError("deployment file is not a disk file")
        before = _file_info(kernel, handle.handle)
        if before.attributes & (_FILE_ATTRIBUTE_DIRECTORY | _FILE_ATTRIBUTE_REPARSE_POINT):
            raise DeploymentFileError("deployment file type is unsupported")
        if before.size <= 0 or before.size > limit:
            raise DeploymentFileError("deployment file size is invalid")
        output = bytearray(before.size)
        offset = 0
        while offset < before.size:
            chunk = min(64 * 1024, before.size - offset)
            buffer = (ctypes.c_ubyte * chunk).from_buffer(output, offset)
            read = wintypes.DWORD()
            if not kernel.ReadFile(handle.handle, buffer, chunk, ctypes.byref(read), None) or read.value <= 0:
                raise DeploymentFileError("deployment file read failed")
            offset += read.value
        extra = (ctypes.c_ubyte * 1)()
        extra_read = wintypes.DWORD()
        if not kernel.ReadFile(handle.handle, extra, 1, ctypes.byref(extra_read), None) or extra_read.value != 0:
            raise DeploymentFileError("deployment file changed during read")
        after = _file_info(kernel, handle.handle)
        if before != after:
            raise DeploymentFileError("deployment file changed during read")
        return bytes(output)


class _WindowsNamespaceBackend(_WindowsReadBackend):
    def __init__(self, after_open=None):
        super().__init__()
        self._namespace_after_open = after_open

    def drive_mapping(self, root: Path) -> str:
        kernel = _kernel32()
        drive = PureWindowsPath(str(root)).drive
        if kernel.GetDriveTypeW(drive + "\\") not in (_DRIVE_FIXED, _DRIVE_REMOVABLE):
            raise DeploymentFileError("deployment drive type is unsupported")
        buffer = ctypes.create_unicode_buffer(1024)
        if not kernel.QueryDosDeviceW(drive, buffer, len(buffer)):
            raise DeploymentFileError("deployment drive mapping unavailable")
        mapping = buffer.value
        if _CANONICAL_DEVICE.fullmatch(mapping) is None:
            raise DeploymentFileError("deployment drive mapping is unsupported")
        return mapping.casefold()

    def open_directory(self, path: Path) -> _WindowsReadHandle:
        return self._open(
            path, _READ_CONTROL | _FILE_READ_ATTRIBUTES, _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT
        )

    def open_file(self, path: Path) -> _WindowsReadHandle:
        return self._open(
            path,
            _GENERIC_READ | _READ_CONTROL | _FILE_READ_ATTRIBUTES,
            _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
        )

    def _open(self, path: Path, access: int, flags: int) -> _WindowsReadHandle:
        handle = _kernel32().CreateFileW(str(path), access, _FILE_SHARE_READ, None, _OPEN_EXISTING, flags, None)
        if handle == _INVALID_HANDLE_VALUE:
            raise DeploymentFileError("deployment namespace handle open failed")
        return _WindowsReadHandle(handle)

    def identity(self, handle: _WindowsReadHandle, *, directory: bool) -> "_FileIdentity":
        if handle.handle is None or _kernel32().GetFileType(handle.handle) != _FILE_TYPE_DISK:
            raise DeploymentFileError("deployment namespace object is not a disk object")
        value = _file_info(_kernel32(), handle.handle)
        if (
            bool(value.attributes & _FILE_ATTRIBUTE_DIRECTORY) != directory
            or value.attributes & _FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise DeploymentFileError("deployment namespace object type mismatch")
        return value

    def volume_facts(self, handle: _WindowsReadHandle) -> "_VolumeFacts":
        serial = wintypes.DWORD()
        flags = wintypes.DWORD()
        filesystem_name = ctypes.create_unicode_buffer(261)
        if handle.handle is None or not _kernel32().GetVolumeInformationByHandleW(
            handle.handle,
            None,
            0,
            ctypes.byref(serial),
            None,
            ctypes.byref(flags),
            filesystem_name,
            len(filesystem_name),
        ):
            raise DeploymentFileError("deployment volume identity unavailable")
        name = filesystem_name.value
        if not name or not name.isascii() or not name.isalpha():
            raise DeploymentFileError("deployment filesystem identity unavailable")
        canonical_name = "NTFS" if name.casefold() == "ntfs" else name.upper()
        return _VolumeFacts(serial.value, bool(flags.value & _FILE_SUPPORTS_PERSISTENT_ACLS), canonical_name)

    def after_all_open(self, ancestors, children) -> None:
        if self._namespace_after_open is not None:
            self._namespace_after_open(ancestors, children)


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    attributes: int
    volume_serial: int
    file_index: int
    size: int
    number_links: int


@dataclass(frozen=True, slots=True)
class _VolumeFacts:
    serial: int
    persistent_acls: bool
    filesystem_name: str


def _file_info(kernel, handle: int) -> _FileIdentity:
    info = _BY_HANDLE_FILE_INFORMATION()
    if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise DeploymentFileError("deployment file identity unavailable")
    return _FileIdentity(
        info.dwFileAttributes,
        info.dwVolumeSerialNumber,
        (info.nFileIndexHigh << 32) | info.nFileIndexLow,
        (info.nFileSizeHigh << 32) | info.nFileSizeLow,
        info.nNumberOfLinks,
    )


class _FILETIME(ctypes.Structure):
    _fields_ = (("low", wintypes.DWORD), ("high", wintypes.DWORD))


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = (
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", _FILETIME),
        ("ftLastAccessTime", _FILETIME),
        ("ftLastWriteTime", _FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    )


def _kernel32():
    if os.name != "nt":
        raise DeploymentFileError("Windows deployment file reader is unavailable")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.ReadFile.argtypes = (
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    )
    kernel.ReadFile.restype = wintypes.BOOL
    kernel.GetFileType.argtypes = (wintypes.HANDLE,)
    kernel.GetFileType.restype = wintypes.DWORD
    kernel.GetFileInformationByHandle.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION),
    )
    kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel.GetFileAttributesW.argtypes = (wintypes.LPCWSTR,)
    kernel.GetFileAttributesW.restype = wintypes.DWORD
    kernel.GetDriveTypeW.argtypes = (wintypes.LPCWSTR,)
    kernel.GetDriveTypeW.restype = wintypes.UINT
    kernel.QueryDosDeviceW.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    kernel.QueryDosDeviceW.restype = wintypes.DWORD
    kernel.GetVolumeInformationByHandleW.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        wintypes.DWORD,
    )
    kernel.GetVolumeInformationByHandleW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def _remember(first: BaseException | None, next_error: BaseException) -> BaseException:
    if first is None:
        return next_error
    if first is next_error:
        return first
    if not isinstance(first, (KeyboardInterrupt, SystemExit)) and isinstance(
        next_error, (KeyboardInterrupt, SystemExit)
    ):
        return next_error
    return first


def _raise_after_cleanup(error: BaseException | None, message: str) -> None:
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        raise error
    raise DeploymentFileError(message) from None
