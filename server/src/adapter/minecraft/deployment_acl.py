"""Read-only, handle-bound Windows ACL inspection for deployment files.

This module verifies a deliberately small owner/protected-DACL policy.  It does
not anchor parent namespaces, mutate ACLs, prove durability, or enable control.
"""

from __future__ import annotations

import ctypes
import os
import struct
import threading
from ctypes import wintypes
from dataclasses import dataclass

_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x1
_DACL_SECURITY_INFORMATION = 0x4
_SE_DACL_PROTECTED = 0x1000
_ACL_SIZE_INFORMATION_CLASS = 2
_ALLOWED = 0
_DENIED = 1
_INHERITED = 0x10
_KNOWN_ACE_FLAGS = 0x1F
_DANGEROUS = 0x10000000 | 0x40000000 | 0x00010000 | 0x00040000 | 0x00080000 | 0x2 | 0x4 | 0x10 | 0x40 | 0x100
_MAX_ACL_BYTES = 4096
_MAX_ACES = 4096


class DeploymentAclError(RuntimeError):
    """Sanitized descriptor/policy error without SID, handle, or native detail."""


@dataclass(frozen=True, slots=True, repr=False)
class HandleAce:
    ace_type: int
    flags: int
    mask: int
    sid: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "sid", _canonical_sid(self.sid))


@dataclass(frozen=True, slots=True, repr=False)
class HandleSecurityDescriptor:
    owner_sid: bytes
    dacl_protected: bool
    aces: tuple[HandleAce, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "owner_sid", _canonical_sid(self.owner_sid))
        if type(self.dacl_protected) is not bool or type(self.aces) is not tuple:
            raise DeploymentAclError("invalid deployment security descriptor")
        if any(not isinstance(ace, HandleAce) for ace in self.aces):
            raise DeploymentAclError("invalid deployment security descriptor")


@dataclass(frozen=True, slots=True, repr=False)
class TrustedDeploymentAclPolicy:
    expected_owner_sid: bytes
    allowed_writer_sids: tuple[bytes, ...]

    def __post_init__(self) -> None:
        owner = _canonical_sid(self.expected_owner_sid)
        if type(self.allowed_writer_sids) is not tuple or not self.allowed_writer_sids:
            raise DeploymentAclError("invalid trusted deployment ACL policy")
        writers = tuple(_canonical_sid(value) for value in self.allowed_writer_sids)
        if len(set(writers)) != len(writers) or owner not in writers:
            raise DeploymentAclError("invalid trusted deployment ACL policy")
        object.__setattr__(self, "expected_owner_sid", owner)
        object.__setattr__(self, "allowed_writer_sids", writers)


@dataclass(frozen=True, slots=True)
class DeploymentAclAssessment:
    accepted: bool
    reason: str


def assess_descriptor(
    descriptor: HandleSecurityDescriptor, policy: TrustedDeploymentAclPolicy
) -> DeploymentAclAssessment:
    """Apply a conservative write-grant policy; DENY ACEs never cancel unsafe ALLOWs."""
    if not isinstance(descriptor, HandleSecurityDescriptor) or not isinstance(policy, TrustedDeploymentAclPolicy):
        raise DeploymentAclError("invalid deployment ACL assessment input")
    if descriptor.owner_sid != policy.expected_owner_sid:
        return DeploymentAclAssessment(False, "deployment owner mismatch")
    if not descriptor.dacl_protected:
        return DeploymentAclAssessment(False, "deployment DACL is not protected")
    allowed = set(policy.allowed_writer_sids)
    for ace in descriptor.aces:
        if ace.ace_type not in (_ALLOWED, _DENIED) or ace.flags & ~_KNOWN_ACE_FLAGS:
            return DeploymentAclAssessment(False, "deployment DACL contains unsupported ACE")
        if ace.ace_type == _ALLOWED and ace.mask & _DANGEROUS:
            if ace.flags & _INHERITED:
                return DeploymentAclAssessment(False, "deployment DACL contains inherited write access")
            if ace.sid not in allowed:
                return DeploymentAclAssessment(False, "deployment DACL grants untrusted write access")
    return DeploymentAclAssessment(True, "deployment handle owner and protected DACL match policy")


@dataclass(frozen=True, slots=True)
class DescriptorAllocationEvidence:
    state: str
    retained: bool
    reason: str


class DescriptorAllocationReceipt:
    """Single-use LocalFree owner. UNKNOWN must be retained; never exposes a native pointer."""

    def __init__(self):
        self.__state = "FRESH"
        self.__descriptor = ctypes.c_void_p()
        self.__owner = ctypes.c_void_p()
        self.__dacl = ctypes.c_void_p()
        self.__api = None
        self.__reason = "NONE"
        self.__lock = threading.RLock()

    def snapshot(self):
        with self.__lock:
            return DescriptorAllocationEvidence(self.__state, bool(self.__descriptor.value), self.__reason)

    def _query(self, handle, api):
        with self.__lock:
            if self.__state != "FRESH":
                raise DeploymentAclError("descriptor allocation is single use")
            self.__state = "QUERYING"
            self.__api = api
        try:
            status = api.get_security_info_owned(handle, self.__owner, self.__dacl, self.__descriptor)
        except BaseException:
            with self.__lock:
                self.__state = "UNKNOWN" if self.__state != "QUERYING" or self.__descriptor.value else "RELEASED"
                self.__reason = "OPAQUE_QUERY_UNKNOWN" if self.__descriptor.value else "QUERY_FAILED"
            raise
        with self.__lock:
            if self.__state != "QUERYING" or status or not self.__descriptor.value:
                self.__state = "UNKNOWN" if self.__state != "QUERYING" or self.__descriptor.value else "RELEASED"
                self.__reason = "OPAQUE_QUERY_UNKNOWN" if self.__descriptor.value else "QUERY_FAILED"
                raise DeploymentAclError(
                    "descriptor query cleanup outcome is unknown"
                    if self.__descriptor.value
                    else "descriptor query failed"
                )
            self.__state = "OWNED"
            return self.__descriptor.value, self.__owner.value, self.__dacl.value

    def close(self):
        with self.__lock:
            if self.__state == "RELEASED":
                return
            if self.__state != "OWNED":
                self.__state = "UNKNOWN"
                self.__reason = "OPAQUE_OR_REENTRANT_CLEANUP"
                raise DeploymentAclError("descriptor cleanup outcome is unknown")
            self.__state = "FREE_IN_PROGRESS"
            pointer, api = self.__descriptor.value, self.__api
        try:
            api.local_free(pointer)
        except BaseException:
            with self.__lock:
                self.__state = "UNKNOWN"
                self.__reason = "LOCAL_FREE_UNKNOWN"
            raise
        with self.__lock:
            if self.__state != "FREE_IN_PROGRESS":
                self.__state = "UNKNOWN"
                raise DeploymentAclError("descriptor cleanup outcome is unknown")
            self.__descriptor.value = None
            self.__owner.value = self.__dacl.value = None
            self.__api = None
            self.__state = "RELEASED"


def inspect_handle_security_descriptor(
    handle: int, *, allocation: DescriptorAllocationReceipt | None = None, _api=None
) -> HandleSecurityDescriptor:
    """Inspect a caller-owned file handle; this function never closes the handle."""
    if os.name != "nt" and _api is None:
        raise DeploymentAclError("Windows deployment ACL inspection is unavailable")
    api = _api or _WindowsSecurityApi()
    descriptor = None
    primary: BaseException | None = None
    result = None
    try:
        if allocation is None:
            descriptor, owner, dacl = api.get_security_info(handle)
        else:
            if type(allocation) is not DescriptorAllocationReceipt:
                raise DeploymentAclError("invalid descriptor allocation receipt")
            descriptor, owner, dacl = allocation._query(handle, api)
        result = _parse_descriptor(api, descriptor, owner, dacl)
    except BaseException as error:
        primary = error
    cleanup_unknown = False
    if descriptor:
        try:
            if allocation is None:
                api.local_free(descriptor)
            else:
                allocation.close()
        except BaseException as error:
            cleanup_unknown = True
            if allocation is not None:
                if primary is None or isinstance(primary, Exception) and not isinstance(error, Exception):
                    primary = error
            else:
                primary = _remember(primary, error)
    _inspection_failure(allocation, primary, cleanup_unknown)
    assert result is not None
    return result


def _parse_descriptor(api, descriptor, owner, dacl):
    if not descriptor or not owner:
        raise DeploymentAclError("deployment owner or descriptor is absent")
    present, actual_dacl = api.get_dacl(descriptor)
    if not present or not actual_dacl or actual_dacl != dacl:
        raise DeploymentAclError("deployment DACL is absent or null")
    control = api.get_control(descriptor)
    return HandleSecurityDescriptor(api.sid_bytes(owner), bool(control & _SE_DACL_PROTECTED), api.aces(actual_dacl))


def _inspection_failure(allocation, primary, cleanup_unknown):
    if allocation is not None and primary is not None and not isinstance(primary, Exception):
        raise primary
    if cleanup_unknown or allocation is not None and allocation.snapshot().state == "UNKNOWN":
        _raise(primary, "deployment descriptor cleanup outcome is unknown")
    if primary is not None:
        _raise(primary, "deployment ACL inspection failed")


def inspect_and_assess_handle(
    handle: int, policy: TrustedDeploymentAclPolicy, *, _api=None
) -> tuple[HandleSecurityDescriptor, DeploymentAclAssessment]:
    descriptor = inspect_handle_security_descriptor(handle, _api=_api)
    return descriptor, assess_descriptor(descriptor, policy)


def _canonical_sid(value: object) -> bytes:
    if type(value) is not bytes or len(value) < 12 or len(value) > 68:
        raise DeploymentAclError("invalid Windows SID")
    result = bytes(value)
    if result[0] != 1 or result[1] > 15 or len(result) != 8 + result[1] * 4:
        raise DeploymentAclError("invalid Windows SID")
    return result


class _WindowsSecurityApi:
    def get_security_info_owned(self, handle, owner, dacl, descriptor):
        return _advapi32().GetSecurityInfo(
            handle,
            _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )

    def get_security_info(self, handle: int):
        owner = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        result = _advapi32().GetSecurityInfo(
            handle,
            _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
        if result:
            raise DeploymentAclError("Windows handle security query failed")
        return descriptor.value, owner.value, dacl.value

    def get_dacl(self, descriptor: int):
        present = wintypes.BOOL()
        dacl = ctypes.c_void_p()
        defaulted = wintypes.BOOL()
        if not _advapi32().GetSecurityDescriptorDacl(
            descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
        ):
            raise DeploymentAclError("Windows DACL query failed")
        return bool(present.value), dacl.value

    def get_control(self, descriptor: int) -> int:
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not _advapi32().GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise DeploymentAclError("Windows descriptor control query failed")
        return control.value

    def sid_bytes(self, sid: int) -> bytes:
        api = _advapi32()
        if not sid or not api.IsValidSid(sid):
            raise DeploymentAclError("invalid Windows SID")
        length = api.GetLengthSid(sid)
        if not 12 <= length <= 68:
            raise DeploymentAclError("invalid Windows SID")
        return _canonical_sid(ctypes.string_at(sid, length))

    def aces(self, dacl: int) -> tuple[HandleAce, ...]:
        info = _AclSizeInformation()
        api = _advapi32()
        if not api.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info), _ACL_SIZE_INFORMATION_CLASS):
            raise DeploymentAclError("invalid Windows ACL")
        if not 8 <= info.AclBytesInUse <= _MAX_ACL_BYTES or info.AceCount > _MAX_ACES:
            raise DeploymentAclError("invalid Windows ACL")
        acl_start = int(dacl)
        acl_end = acl_start + info.AclBytesInUse
        if acl_end < acl_start:
            raise DeploymentAclError("invalid Windows ACL")
        result = []
        for index in range(info.AceCount):
            pointer = ctypes.c_void_p()
            if not api.GetAce(dacl, index, ctypes.byref(pointer)) or not pointer.value:
                raise DeploymentAclError("invalid Windows ACL")
            ace_start = int(pointer.value)
            if ace_start < acl_start + 8 or ace_start > acl_end - 8:
                raise DeploymentAclError("invalid Windows ACE")
            header = ctypes.string_at(ace_start, 8)
            ace_type, flags, size, mask = struct.unpack("<BBHI", header)
            if (
                ace_type not in (_ALLOWED, _DENIED)
                or size < 20
                or size > info.AclBytesInUse
                or ace_start > acl_end - size
            ):
                raise DeploymentAclError("unsupported Windows ACE")
            raw_ace = ctypes.string_at(ace_start, size)
            sid = _canonical_sid(raw_ace[8:])
            if len(sid) != size - 8:
                raise DeploymentAclError("invalid Windows ACE")
            result.append(HandleAce(ace_type, flags, mask, sid))
        return tuple(result)

    def local_free(self, descriptor: int) -> None:
        if _kernel32().LocalFree(descriptor):
            raise DeploymentAclError("Windows descriptor cleanup outcome is unknown")


class _AclSizeInformation(ctypes.Structure):
    _fields_ = (("AceCount", wintypes.DWORD), ("AclBytesInUse", wintypes.DWORD), ("AclBytesFree", wintypes.DWORD))


def _kernel32():
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    library.LocalFree.argtypes = (ctypes.c_void_p,)
    library.LocalFree.restype = ctypes.c_void_p
    return library


def _advapi32():
    library = ctypes.WinDLL("advapi32", use_last_error=True)
    library.GetSecurityInfo.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    )
    library.GetSecurityInfo.restype = wintypes.DWORD
    library.GetSecurityDescriptorDacl.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    )
    library.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    library.GetSecurityDescriptorControl.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    )
    library.GetSecurityDescriptorControl.restype = wintypes.BOOL
    library.IsValidSid.argtypes = (ctypes.c_void_p,)
    library.IsValidSid.restype = wintypes.BOOL
    library.GetLengthSid.argtypes = (ctypes.c_void_p,)
    library.GetLengthSid.restype = wintypes.DWORD
    library.GetAclInformation.argtypes = (ctypes.c_void_p, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD)
    library.GetAclInformation.restype = wintypes.BOOL
    library.GetAce.argtypes = (ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))
    library.GetAce.restype = wintypes.BOOL
    return library


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


def _raise(error: BaseException | None, message: str) -> None:
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        raise error
    raise DeploymentAclError(message) from None
