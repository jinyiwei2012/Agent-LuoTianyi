"""Windows-only, non-authorizing reader for frozen deployment artifact bytes.

This module proves only that four explicitly supplied local files were read from
stable no-write-share handles and matched a trusted manifest identity.  Parent
namespace anchoring, ACL authorization, durability, credentials, and activation
remain deliberately unverified.
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

from .deployment_manifest import (
    TrustedDeploymentArtifacts,
    TrustedDeploymentIdentity,
    parse_deployment_manifest,
    verify_trusted_deployment,
)

_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_TYPE_DISK = 0x0001
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_INVALID_ATTRIBUTES = 0xFFFFFFFF
_MANIFEST_LIMIT = 4096
_ARTIFACT_LIMIT = 1_048_576
_DRIVE = re.compile(r"[A-Za-z]:\\")


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


def _validate_windows_path(path: Path) -> None:
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


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    attributes: int
    volume_serial: int
    file_index: int
    size: int


def _file_info(kernel, handle: int) -> _FileIdentity:
    info = _BY_HANDLE_FILE_INFORMATION()
    if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise DeploymentFileError("deployment file identity unavailable")
    return _FileIdentity(
        info.dwFileAttributes,
        info.dwVolumeSerialNumber,
        (info.nFileIndexHigh << 32) | info.nFileIndexLow,
        (info.nFileSizeHigh << 32) | info.nFileSizeLow,
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
