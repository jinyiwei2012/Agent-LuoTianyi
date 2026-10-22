"""Read-only conservative Windows owner/DACL verification for epoch files.

Only common ACCESS_ALLOWED/ACCESS_DENIED ACEs are understood.  Any descriptor
feature outside that small allowlist is rejected rather than interpreted loosely.
"""

from __future__ import annotations

import ctypes
import os
import struct
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from src.adapter.minecraft.epoch_store import EpochStoreError

_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x1
_DACL_SECURITY_INFORMATION = 0x4
_TOKEN_QUERY = 0x8
_TOKEN_USER = 1
_ACL_SIZE_INFORMATION_CLASS = 2
_ACCESS_ALLOWED_ACE_TYPE = 0
_ACCESS_DENIED_ACE_TYPE = 1
_INHERITED_ACE = 0x10
_WRITE_MASK = 0x10000000 | 0x40000000 | 0x00010000 | 0x00040000 | 0x00080000 | 0x2 | 0x4 | 0x10 | 0x40 | 0x100


@dataclass(frozen=True, slots=True)
class Ace:
    ace_type: int
    flags: int
    mask: int
    sid: bytes


@dataclass(frozen=True, slots=True)
class AclAssessment:
    verified: bool
    reason: str


def assess_descriptor(owner_sid: bytes | None, service_sid: bytes, dacl: tuple[Ace, ...] | None) -> AclAssessment:
    """Verify owner identity and reject non-owner write grants conservatively."""
    if not owner_sid or owner_sid != service_sid:
        return AclAssessment(False, "epoch owner does not match the service account")
    if dacl is None:
        return AclAssessment(False, "epoch DACL is absent")
    for ace in dacl:
        if ace.ace_type not in (_ACCESS_ALLOWED_ACE_TYPE, _ACCESS_DENIED_ACE_TYPE) or not ace.sid:
            return AclAssessment(False, "epoch DACL contains an unsupported ACE")
        if ace.ace_type == _ACCESS_ALLOWED_ACE_TYPE and ace.mask & _WRITE_MASK and ace.sid != service_sid:
            return AclAssessment(False, "epoch DACL grants write access outside the service account")
        if ace.flags & _INHERITED_ACE and ace.ace_type == _ACCESS_ALLOWED_ACE_TYPE and ace.mask & _WRITE_MASK:
            return AclAssessment(False, "epoch DACL inherits write access")
    return AclAssessment(True, "epoch owner and DACL are conservatively verified")


class WindowsAclVerifier:
    """Uses Windows security APIs read-only and releases each security descriptor."""

    def assess_path(self, path: Path) -> AclAssessment:
        if os.name != "nt" or path.is_symlink():
            return AclAssessment(False, "Windows ACL verification is unavailable")
        try:
            service_sid = _current_user_sid()
            owner, dacl = _named_descriptor(Path(path))
            return assess_descriptor(owner, service_sid, dacl)
        except EpochStoreError:
            return AclAssessment(False, "Windows ACL verification failed")


class _AclSizeInformation(ctypes.Structure):
    _fields_ = [("AceCount", wintypes.DWORD), ("AclBytesInUse", wintypes.DWORD), ("AclBytesFree", wintypes.DWORD)]


class _TOKEN_USER_STRUCT(ctypes.Structure):
    _fields_ = [("User", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


def _current_user_sid() -> bytes:
    advapi32 = _advapi32()
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(_kernel32().GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise EpochStoreError("Windows token query failed")
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
        if not size.value:
            raise EpochStoreError("Windows token query failed")
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, _TOKEN_USER, buffer, size, ctypes.byref(size)):
            raise EpochStoreError("Windows token query failed")
        return _sid_bytes(ctypes.cast(buffer, ctypes.POINTER(_TOKEN_USER_STRUCT)).contents.User, size.value)
    finally:
        _kernel32().CloseHandle(token)


def _named_descriptor(path: Path) -> tuple[bytes, tuple[Ace, ...] | None]:
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    result = _advapi32().GetNamedSecurityInfoW(
        str(path),
        _SE_FILE_OBJECT,
        _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if result:
        raise EpochStoreError("Windows security descriptor query failed")
    try:
        if not owner.value or not dacl.value:
            return b"", None
        return _sid_bytes(owner.value, 0), _acl_aces(dacl.value)
    finally:
        if descriptor.value:
            _kernel32().LocalFree(descriptor)


def _sid_bytes(sid: int, available: int) -> bytes:
    advapi32 = _advapi32()
    if not sid or not advapi32.IsValidSid(sid):
        raise EpochStoreError("invalid Windows SID")
    size = advapi32.GetLengthSid(sid)
    if not size or (available and size > available):
        raise EpochStoreError("invalid Windows SID")
    return ctypes.string_at(sid, size)


def _acl_aces(dacl: int) -> tuple[Ace, ...]:
    info = _AclSizeInformation()
    advapi32 = _advapi32()
    if not advapi32.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info), _ACL_SIZE_INFORMATION_CLASS):
        raise EpochStoreError("invalid Windows ACL")
    if info.AclBytesInUse < 8 or info.AceCount > 4096:
        raise EpochStoreError("invalid Windows ACL")
    result: list[Ace] = []
    for index in range(info.AceCount):
        ace_ptr = ctypes.c_void_p()
        if not advapi32.GetAce(dacl, index, ctypes.byref(ace_ptr)) or not ace_ptr.value:
            raise EpochStoreError("invalid Windows ACL")
        header = ctypes.string_at(ace_ptr.value, 8)
        ace_type, flags, ace_size, mask = struct.unpack("<BBHI", header)
        if ace_size < 16 or ace_size > info.AclBytesInUse:
            raise EpochStoreError("invalid Windows ACL")
        sid = _sid_bytes(ace_ptr.value + 8, ace_size - 8)
        result.append(Ace(ace_type, flags, mask, sid))
    return tuple(result)


def _kernel32():
    if os.name != "nt":
        raise EpochStoreError("Windows ACL verification is unavailable")
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    library.GetCurrentProcess.argtypes = ()
    library.GetCurrentProcess.restype = wintypes.HANDLE
    library.CloseHandle.argtypes = (wintypes.HANDLE,)
    library.CloseHandle.restype = wintypes.BOOL
    library.LocalFree.argtypes = (ctypes.c_void_p,)
    library.LocalFree.restype = ctypes.c_void_p
    return library


def _advapi32():
    if os.name != "nt":
        raise EpochStoreError("Windows ACL verification is unavailable")
    library = ctypes.WinDLL("advapi32", use_last_error=True)
    library.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    library.OpenProcessToken.restype = wintypes.BOOL
    library.GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    library.GetTokenInformation.restype = wintypes.BOOL
    library.GetNamedSecurityInfoW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    )
    library.GetNamedSecurityInfoW.restype = wintypes.DWORD
    library.IsValidSid.argtypes = (ctypes.c_void_p,)
    library.IsValidSid.restype = wintypes.BOOL
    library.GetLengthSid.argtypes = (ctypes.c_void_p,)
    library.GetLengthSid.restype = wintypes.DWORD
    library.GetAclInformation.argtypes = (ctypes.c_void_p, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD)
    library.GetAclInformation.restype = wintypes.BOOL
    library.GetAce.argtypes = (ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))
    library.GetAce.restype = wintypes.BOOL
    return library
