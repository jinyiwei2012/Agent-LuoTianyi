"""Conservative Windows capability probe and lifetime no-share epoch lock.

This module never changes ACLs or creates epoch files.  It deliberately reports
unverified until a separately reviewed ACL descriptor verifier is supplied.
"""

from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from src.adapter.minecraft.epoch_store import EpochStoreError, PlatformCapability

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_ERROR_SHARING_VIOLATION = 32
_DRIVE_REMOTE = 4
_FILE_SUPPORTS_PERSISTENT_ACLS = 0x00000008


@dataclass(frozen=True, slots=True)
class VolumeFacts:
    """Read-only facts collected from the path's Windows volume."""

    local: bool
    persistent_acls: bool
    reparse_point: bool


class WindowsEpochPlatform:
    """Windows-only lock backend with conservative, fail-closed ACL capability.

    The volume probe is real, but it is not an ACL authorization proof.  Since
    this bounded module intentionally does not evaluate owner/DACL/ACE semantics,
    ``capability`` always remains unverified even on a suitable local volume.
    """

    def capability(self, directory: Path) -> PlatformCapability:
        facts = self.volume_facts(directory)
        if not facts.local:
            return PlatformCapability(False, "epoch directory is not on a local volume")
        if facts.reparse_point:
            return PlatformCapability(False, "epoch directory is a reparse point")
        if not facts.persistent_acls:
            return PlatformCapability(False, "epoch volume does not report persistent ACL support")
        return PlatformCapability(False, "epoch owner and DACL verification is not implemented")

    def acquire_lock(self, lock_path: Path) -> "WindowsLockHandle":
        return WindowsLockHandle.acquire(lock_path)

    def volume_facts(self, directory: Path) -> VolumeFacts:
        if os.name != "nt":
            return VolumeFacts(False, False, False)
        path = str(directory.resolve())
        kernel32 = _kernel32()
        root = ctypes.create_unicode_buffer(261)
        if not kernel32.GetVolumePathNameW(path, root, len(root)):
            raise EpochStoreError("Windows volume probe failed")
        drive_type = kernel32.GetDriveTypeW(root.value)
        flags = wintypes.DWORD()
        if not kernel32.GetVolumeInformationW(root.value, None, 0, None, None, ctypes.byref(flags), None, 0):
            raise EpochStoreError("Windows volume probe failed")
        attributes = kernel32.GetFileAttributesW(path)
        if attributes == 0xFFFFFFFF:
            raise EpochStoreError("Windows path probe failed")
        return VolumeFacts(
            local=drive_type != _DRIVE_REMOTE,
            persistent_acls=bool(flags.value & _FILE_SUPPORTS_PERSISTENT_ACLS),
            reparse_point=bool(attributes & 0x400),
        )


class WindowsLockHandle:
    """An idempotently closable CreateFileW no-share handle owned by this object."""

    def __init__(self, handle: int):
        self._handle: int | None = handle
        self._close_state = "ACTIVE"
        self._close_lock = threading.RLock()

    @classmethod
    def acquire(cls, lock_path: Path) -> "WindowsLockHandle":
        if os.name != "nt" or not lock_path.is_file() or lock_path.is_symlink():
            raise EpochStoreError("Windows epoch lock is unavailable")
        handle = _kernel32().CreateFileW(
            str(lock_path),
            _GENERIC_READ | _GENERIC_WRITE,
            0,
            None,
            _OPEN_EXISTING,
            _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            error = ctypes.get_last_error()
            if error == _ERROR_SHARING_VIOLATION:
                raise EpochStoreError("epoch lock is already held")
            raise EpochStoreError("Windows epoch lock acquisition failed")
        return cls(handle)

    def close(self) -> None:
        with self._close_lock:
            if self._close_state == "RELEASED":
                return
            if self._close_state != "ACTIVE":
                self._close_state = "CLEANUP_UNKNOWN"
                raise EpochStoreError("Windows epoch lock cleanup outcome is unknown")
            self._close_state = "CLOSE_IN_PROGRESS"
            handle = self._handle
            try:
                released = bool(_kernel32().CloseHandle(handle))
            except BaseException as error:
                self._close_state = "CLEANUP_UNKNOWN"
                if not isinstance(error, Exception):
                    raise
                raise EpochStoreError("Windows epoch lock cleanup outcome is unknown") from error
            if self._close_state != "CLOSE_IN_PROGRESS" or not released:
                # Keep the numeric value for diagnostics only. CloseHandle may have
                # invalidated it despite reporting failure, so it must never be retried.
                self._close_state = "CLEANUP_UNKNOWN"
                raise EpochStoreError("Windows epoch lock cleanup outcome is unknown")
            self._handle = None
            self._close_state = "RELEASED"


def _kernel32():
    if os.name != "nt":
        raise EpochStoreError("Windows epoch platform is unavailable")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetVolumePathNameW.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    kernel32.GetVolumePathNameW.restype = wintypes.BOOL
    kernel32.GetDriveTypeW.argtypes = (wintypes.LPCWSTR,)
    kernel32.GetDriveTypeW.restype = wintypes.UINT
    kernel32.GetVolumeInformationW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        wintypes.DWORD,
    )
    kernel32.GetVolumeInformationW.restype = wintypes.BOOL
    kernel32.GetFileAttributesW.argtypes = (wintypes.LPCWSTR,)
    kernel32.GetFileAttributesW.restype = wintypes.DWORD
    return kernel32
