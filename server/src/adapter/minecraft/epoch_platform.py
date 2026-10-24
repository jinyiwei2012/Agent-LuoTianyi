"""Conservative Windows capability probe and lifetime no-share epoch lock.

This module never changes ACLs or creates epoch files.  It deliberately reports
unverified until a separately reviewed ACL descriptor verifier is supplied.
"""

from __future__ import annotations

import ctypes
import os
import threading
from collections import Counter
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from src.adapter.minecraft.deployment_acl import (
    DescriptorAllocationReceipt,
    HandleSecurityDescriptor,
    inspect_handle_security_descriptor,
)
from src.adapter.minecraft.epoch_store import (
    EpochOpenAttemptReceipt,
    EpochOpenLease,
    EpochStoreError,
    PlatformCapability,
    TrustedEpochPolicy,
)

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_ERROR_SHARING_VIOLATION = 32
_DRIVE_REMOTE = 4
_FILE_SUPPORTS_PERSISTENT_ACLS = 0x00000008


@dataclass(frozen=True, slots=True, repr=False)
class NativeEpochOpenProfile:
    expected_token_owner_sid: bytes
    journal_policy: str

    def __post_init__(self):
        HandleSecurityDescriptor(self.expected_token_owner_sid, True, ())
        if self.journal_policy != "EXPLICIT_INHERITABLE_ALLOW_V1":
            raise EpochStoreError("unsupported epoch journal policy")


def _native_paths(policy):
    policy.__post_init__()
    raw = policy.raw_root_path
    try:
        units = len(raw.encode("utf-16-le", errors="strict")) // 2
    except UnicodeError:
        raise EpochStoreError("invalid epoch UTF16 path") from None
    parts = PureWindowsPath(raw).parts
    if units > 259 or len(parts) - 1 > 32:
        raise EpochStoreError("epoch path exceeds native bounds")
    directories = [parts[0]]
    for part in parts[1:]:
        directories.append(str(PureWindowsPath(directories[-1]) / part))
    return directories + [
        str(PureWindowsPath(raw) / name) for name in ("epochs.lock", "provision.json", "epochs.sqlite3")
    ]


def _expanded(mask, directory):
    if type(mask) is not int or not 0 <= mask <= 0xFFFFFFFF:
        raise EpochStoreError("unsupported epoch access mask")
    for generic, specific in (
        (0x80000000, 0x120089),
        (0x40000000, 0x120116),
        (0x20000000, 0x1200A0),
        (0x10000000, 0x1F01FF),
    ):
        if mask & generic:
            mask = (mask & ~generic) | specific
    if mask & ~0x1F01FF or not directory and mask & 0x40:
        raise EpochStoreError("unsupported epoch access mask")
    return mask


def _assess_aces(aces, policy, directory, *, inherited=False):
    for ace in aces:
        if ace.ace_type != 0 or ace.flags not in ((0x10,) if inherited else (0, 3) if directory else (0,)):
            raise EpochStoreError("unsupported epoch ACE language")
        # S-1-3-* creator/substitution principals are deliberately unsupported.
        if ace.sid[2:8] == b"\0\0\0\0\0\x03":
            raise EpochStoreError("epoch substitution principal is unsupported")
        mask = _expanded(ace.mask, directory)
        content = 0x110 if directory else 0x116
        namespace = 0x10046 if directory else 0x10000
        for touched, trusted in (
            (mask & content, policy.content_writer_sids),
            (mask & namespace, policy.namespace_mutator_sids),
            (mask & 0xC0000, policy.security_mutator_sids),
        ):
            if touched and ace.sid not in trusted:
                raise EpochStoreError("epoch mutation authority is not trusted")


def _assess_epoch(descriptor, policy, directory, root_or_file):
    if not isinstance(descriptor, HandleSecurityDescriptor) or not descriptor.dacl_protected:
        raise EpochStoreError("epoch DACL must be protected and nonnull")
    if descriptor.owner_sid not in policy.security_mutator_sids:
        raise EpochStoreError("epoch owner security authority is not trusted")
    if root_or_file and descriptor.owner_sid != policy.expected_owner_sid:
        raise EpochStoreError("epoch fixed object owner mismatch")
    _assess_aces(descriptor.aces, policy, directory)


def _journal_prediction(descriptor, policy, profile, token_owner):
    from src.adapter.minecraft.deployment_acl import HandleAce

    if token_owner != profile.expected_token_owner_sid or token_owner not in policy.security_mutator_sids:
        raise EpochStoreError("epoch effective token owner mismatch")
    predicted = tuple(HandleAce(0, 0x10, ace.mask, ace.sid) for ace in descriptor.aces if ace.flags == 3)
    if not predicted:
        raise EpochStoreError("epoch journal inheritance is unavailable")
    _assess_aces(predicted, policy, False, inherited=True)
    return token_owner, tuple(
        sorted((ace.ace_type, ace.flags, _expanded(ace.mask, False), ace.sid) for ace in predicted)
    )


def _match_journal(actual, prediction, policy):
    _assess_aces(actual.aces, policy, False, inherited=True)
    actual_aces = Counter((ace.ace_type, ace.flags, _expanded(ace.mask, False), ace.sid) for ace in actual.aces)
    if actual.owner_sid != prediction[0] or actual_aces != Counter(prediction[1]):
        raise EpochStoreError("epoch journal descriptor does not match prediction")


class _TokenAllocation:
    def __init__(self):
        self.output = wintypes.HANDLE()
        self.state = "QUERYING"

    def establish(self, success):
        value = self.output.value
        if self.state != "QUERYING" or not success:
            self.state = "OPAQUE_UNKNOWN" if value or self.state != "QUERYING" else "RELEASED"
            return False
        if value in (None, 0, _INVALID_HANDLE_VALUE):
            self.state = "RELEASED" if not value else "OPAQUE_UNKNOWN"
            raise EpochStoreError("invalid successful epoch token output")
        self.state = "OWNED"
        return True

    def close(self, api):
        if self.state == "RELEASED":
            return
        if self.state != "OWNED":
            self.state = "OPAQUE_UNKNOWN" if self.state == "QUERYING" else "UNKNOWN"
            raise EpochStoreError("epoch token cleanup outcome is unknown")
        self.state = "CLOSING"
        try:
            api.close(self.output.value)
        except BaseException:
            self.state = "UNKNOWN"
            raise
        if self.state != "CLOSING":
            self.state = "UNKNOWN"
            raise EpochStoreError("epoch token cleanup outcome is unknown")
        self.output.value = None
        self.state = "RELEASED"


class WindowsEpochCustodyPlatform:
    """Explicit restricted custody only; never changes production capability or provisions files."""

    def __init__(self, profile: NativeEpochOpenProfile):
        if type(profile) is not NativeEpochOpenProfile:
            raise EpochStoreError("invalid native epoch profile")
        self._profile = profile

    def acquire(self, policy: TrustedEpochPolicy, receipt: EpochOpenAttemptReceipt) -> EpochOpenLease:
        if type(policy) is not TrustedEpochPolicy or type(receipt) is not EpochOpenAttemptReceipt:
            raise EpochStoreError("invalid native epoch custody input")
        receipt._registration_window()
        paths = _native_paths(policy)
        self._profile.__post_init__()
        lease = _NativeEpochLease(policy, self._profile, paths)
        receipt._register(lease)  # Empty composite owns every subsequent native result immediately.
        lease.acquire()
        return lease


class _NativeEpochLease:
    def __init__(self, policy, profile, paths):
        self._policy, self._profile, self._paths = policy, profile, paths
        self._handles = [None] * len(paths)
        self._attempted = [False] * len(paths)
        self._baseline = [None] * len(paths)
        self._state = "EMPTY"
        self._lock = threading.RLock()
        self._api = None
        self._descriptor_allocation = None
        self._mapping = None
        self._tokens = [None, None]
        self._operation = None
        self._close_started = False

    def acquire(self):
        if self._operation is not None or self._state != "EMPTY":
            raise EpochStoreError("epoch custody acquisition is single use")
        token = self._operation = object()
        self._state = "ACQUIRING"
        self._api = _EpochNativeApi(self)
        try:
            self._mapping = self._api.mapping(self._paths[0])
            self._current("ACQUIRING", token)
            for index, path in enumerate(self._paths):
                directory = index < len(self._paths) - 3
                kind = "directory" if directory else ("lock", "marker", "db")[index - len(self._paths) + 3]
                self._handles[index] = self._api.open(path, kind)
                self._current("ACQUIRING", token)
                self._baseline[index] = self._inspect(index)
                self._current("ACQUIRING", token)
            self._state = "ACTIVE"
            self._operation = None
            self.validate()
        except BaseException:
            self._invalidate()
            raise
        finally:
            self._operation = None

    def _invalidate(self):
        if self._state not in {"CLOSED", "UNKNOWN"}:
            self._state = "INVALID"

    def _current(self, state, token=None):
        if self._state != state or token is not None and self._operation is not token:
            raise EpochStoreError("epoch custody operation invalidated")

    def _inspect(self, index):
        expected = self._state
        token = self._operation
        info = self._api.info(self._handles[index])
        self._current(expected, token)
        directory = index < len(self._paths) - 3
        if (
            info["volume"] != self._policy.expected_volume_serial
            or not info["index"]
            or info["directory"] != directory
            or info["reparse"]
            or info["delete_pending"]
            or not directory
            and info["links"] != 1
        ):
            raise EpochStoreError("epoch held object identity is unsupported")
        root = index == len(self._paths) - 4
        if root and info["index"] != self._policy.expected_file_index:
            raise EpochStoreError("epoch root identity mismatch")
        if self._descriptor_allocation is not None and self._descriptor_allocation.snapshot().state != "RELEASED":
            raise EpochStoreError("descriptor cleanup outcome is unknown")
        self._descriptor_allocation = DescriptorAllocationReceipt()
        descriptor = self._api.descriptor(self._handles[index], self._descriptor_allocation)
        self._current(expected, token)
        _assess_epoch(descriptor, self._policy, directory, root or not directory)
        if root:
            _journal_prediction(descriptor, self._policy, self._profile, self._api.token_owner())
            self._current(expected, token)
        return info, descriptor

    def validate(self):
        with self._lock:
            if self._operation is not None:
                self._invalidate()
                raise EpochStoreError("reentrant epoch custody operation")
            if self._state != "ACTIVE":
                raise EpochStoreError("epoch custody is not operational")
            try:
                token = self._operation = object()
                if self._api.mapping(self._paths[0]) != self._mapping:
                    raise EpochStoreError("epoch DOS mapping changed")
                self._current("ACTIVE", token)
                for index in range(len(self._handles)):
                    if self._inspect(index) != self._baseline[index] or self._state != "ACTIVE":
                        raise EpochStoreError("epoch held object changed")
            except BaseException:
                self._invalidate()
                raise
            finally:
                self._operation = None

    def read_marker(self):
        with self._lock:
            self.validate()
            try:
                token = self._operation = object()
                handle = self._handles[-2]
                before = self._api.size(handle)
                self._current("ACTIVE", token)
                if before > 4096:
                    raise EpochStoreError("epoch marker exceeds bound")
                raw = self._api.read_marker(handle)
                self._current("ACTIVE", token)
                after = self._api.size(handle)
                self._current("ACTIVE", token)
                if len(raw) != before or len(raw) > 4096 or after != before:
                    raise EpochStoreError("epoch marker changed")
                self._current("ACTIVE")
                self._operation = None
                self.validate()
                return raw
            except BaseException:
                self._invalidate()
                raise
            finally:
                self._operation = None

    def close(self):
        with self._lock:
            if self._state == "CLOSED":
                return
            if self._close_started:
                self._state = "UNKNOWN"
                raise EpochStoreError("epoch custody cleanup outcome is unknown")
            if self._operation is not None:
                self._state = "UNKNOWN"
                raise EpochStoreError("epoch custody cleanup outcome is unknown")
            self._close_started = True
            if self._state != "UNKNOWN":
                self._state = "CLOSING"
            first = self._close_temporary()
            for index in reversed(range(len(self._handles))):
                if self._handles[index] is None or self._attempted[index]:
                    continue
                self._attempted[index] = True
                try:
                    self._api.close(self._handles[index])
                except BaseException as error:
                    self._state = "UNKNOWN"
                    if first is None or isinstance(first, Exception) and not isinstance(error, Exception):
                        first = error
                else:
                    self._handles[index] = None
            self._finish_close(first)

    def _finish_close(self, first):
        if self._state == "UNKNOWN":
            if first is not None and not isinstance(first, Exception):
                raise first
            raise EpochStoreError("epoch custody cleanup outcome is unknown")
        self._state = "CLOSED"

    def _close_temporary(self):
        first = None
        for allocation in reversed(self._tokens):
            if allocation is None:
                continue
            try:
                allocation.close(self._api)
            except BaseException as error:
                self._state = "UNKNOWN"
                first = _EpochNativeApi._first_error(first, error)
        if self._descriptor_allocation is not None:
            try:
                self._descriptor_allocation.close()
            except BaseException as error:
                self._state = "UNKNOWN"
                if first is None or isinstance(first, Exception) and not isinstance(error, Exception):
                    first = error
        return first


class _EpochNativeApi:
    """Bounded Win32 implementation; instantiated only after registered composite ownership."""

    def __init__(self, lease):
        self._lease = lease
        self.k = _kernel32()
        self.k.GetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
        self.k.GetFileInformationByHandle.restype = wintypes.BOOL
        self.k.GetFileInformationByHandleEx.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        self.k.GetFileInformationByHandleEx.restype = wintypes.BOOL
        self.k.GetFileType.argtypes = (wintypes.HANDLE,)
        self.k.GetFileType.restype = wintypes.DWORD
        self.k.QueryDosDeviceW.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
        self.k.QueryDosDeviceW.restype = wintypes.DWORD
        self.k.GetFileSizeEx.argtypes = (wintypes.HANDLE, ctypes.POINTER(ctypes.c_int64))
        self.k.GetFileSizeEx.restype = wintypes.BOOL
        self.k.SetFilePointerEx.argtypes = (wintypes.HANDLE, ctypes.c_int64, ctypes.c_void_p, wintypes.DWORD)
        self.k.SetFilePointerEx.restype = wintypes.BOOL
        self.k.ReadFile.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p)
        self.k.ReadFile.restype = wintypes.BOOL

    def mapping(self, root):
        buffer = ctypes.create_unicode_buffer(1024)
        count = self.k.QueryDosDeviceW(root[:2], buffer, len(buffer))
        raw = "".join(buffer[:count]) if count else ""
        import re

        if (
            not count
            or count >= len(buffer)
            or not raw.endswith("\0\0")
            or not re.fullmatch(r"\\Device\\HarddiskVolume[1-9][0-9]*\x00\x00", raw)
        ):
            raise EpochStoreError("unsupported epoch DOS mapping")
        if self.k.GetDriveTypeW(root) != 3:
            raise EpochStoreError("epoch volume must be fixed local NTFS")
        fs = ctypes.create_unicode_buffer(32)
        if not self.k.GetVolumeInformationW(root, None, 0, None, None, None, fs, len(fs)) or fs.value != "NTFS":
            raise EpochStoreError("epoch volume must be fixed local NTFS")
        return raw

    def open(self, path, kind):
        access, share = {
            "directory": (0x20080, 3),
            "lock": (0xC0020080, 0),
            "marker": (0x80020080, 1),
            "db": (0x20080, 3),
        }[kind]
        flags = 0x200000 | (0x2000000 if kind == "directory" else 0)
        handle = self.k.CreateFileW(path, access, share, None, 3, flags, None)
        if handle in (None, _INVALID_HANDLE_VALUE):
            raise EpochStoreError("epoch native open failed")
        return handle

    def info(self, handle):
        class Info(ctypes.Structure):
            _fields_ = [
                ("attributes", wintypes.DWORD),
                ("times", wintypes.DWORD * 6),
                ("volume", wintypes.DWORD),
                ("size_high", wintypes.DWORD),
                ("size_low", wintypes.DWORD),
                ("links", wintypes.DWORD),
                ("index_high", wintypes.DWORD),
                ("index_low", wintypes.DWORD),
            ]

        class Standard(ctypes.Structure):
            _fields_ = [
                ("allocation", ctypes.c_int64),
                ("end", ctypes.c_int64),
                ("links", wintypes.DWORD),
                ("delete", ctypes.c_ubyte),
                ("directory", ctypes.c_ubyte),
            ]

        info, standard = Info(), Standard()
        if (
            self.k.GetFileType(handle) != 1
            or not self.k.GetFileInformationByHandle(handle, ctypes.byref(info))
            or not self.k.GetFileInformationByHandleEx(handle, 1, ctypes.byref(standard), ctypes.sizeof(standard))
        ):
            raise EpochStoreError("epoch identity query failed")
        return dict(
            volume=info.volume,
            index=(info.index_high << 32) | info.index_low,
            directory=bool(standard.directory),
            reparse=bool(info.attributes & 0x400),
            delete_pending=bool(standard.delete),
            links=standard.links,
        )

    def descriptor(self, handle, allocation):
        return inspect_handle_security_descriptor(handle, allocation=allocation)

    def token_owner(self):
        self._token_available()
        api = ctypes.WinDLL("advapi32", use_last_error=True)
        api.OpenThreadToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, ctypes.c_void_p)
        api.OpenThreadToken.restype = wintypes.BOOL
        api.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p)
        api.OpenProcessToken.restype = wintypes.BOOL
        api.GetTokenInformation.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
        )
        api.GetTokenInformation.restype = wintypes.BOOL
        self.k.GetCurrentThread.restype = wintypes.HANDLE
        self.k.GetCurrentProcess.restype = wintypes.HANDLE
        allocation = None
        first = None
        result = None
        try:
            allocation = self._open_token(api)
            buffer = ctypes.create_string_buffer(256)
            size = wintypes.DWORD()
            expected, token = self._lease._state, self._lease._operation
            if not api.GetTokenInformation(allocation.output, 4, buffer, len(buffer), ctypes.byref(size)):
                raise EpochStoreError("epoch TokenOwner query failed")
            self._lease._current(expected, token)
            result = self._owner_bytes(buffer, size.value)
        except BaseException as error:
            first = error
        if allocation is not None:
            try:
                allocation.close(self)
            except BaseException as error:
                first = self._first_error(first, error)
        if first is not None:
            raise first
        return result

    def _token_available(self):
        if any(item is not None and item.state != "RELEASED" for item in self._lease._tokens):
            raise EpochStoreError("epoch token cleanup outcome is unknown")

    def _open_token(self, api):
        for index in range(2):
            allocation = _TokenAllocation()
            self._lease._tokens[index] = allocation
            state, token = self._lease._state, self._lease._operation
            try:
                if index == 0:
                    success = api.OpenThreadToken(self.k.GetCurrentThread(), 8, True, ctypes.byref(allocation.output))
                else:
                    success = api.OpenProcessToken(self.k.GetCurrentProcess(), 8, ctypes.byref(allocation.output))
                error = ctypes.get_last_error() if not success else 0
            except BaseException:
                allocation.establish(False)
                raise
            owned = allocation.establish(bool(success))
            self._lease._current(state, token)
            if owned:
                return allocation
            if allocation.state != "RELEASED" or index or error != 1008:
                raise EpochStoreError(
                    "epoch token acquisition outcome is unknown"
                    if allocation.state != "RELEASED"
                    else "epoch token unavailable"
                )
        raise EpochStoreError("epoch token unavailable")

    @staticmethod
    def _first_error(first, error):
        return error if first is None or isinstance(first, Exception) and not isinstance(error, Exception) else first

    @staticmethod
    def _owner_bytes(buffer, size):
        pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        base = ctypes.addressof(buffer)
        if not pointer or pointer < base or pointer + 8 > base + min(size, len(buffer)):
            raise EpochStoreError("epoch TokenOwner buffer is invalid")
        header = ctypes.string_at(pointer, 8)
        length = 8 + header[1] * 4
        if header[1] > 15 or pointer + length > base + min(size, len(buffer)):
            raise EpochStoreError("epoch TokenOwner SID is invalid")
        result = ctypes.string_at(pointer, length)
        HandleSecurityDescriptor(result, True, ())
        return result

    def size(self, handle):
        value = ctypes.c_int64()
        if not self.k.GetFileSizeEx(handle, ctypes.byref(value)) or value.value < 0:
            raise EpochStoreError("epoch marker size query failed")
        return value.value

    def read_marker(self, handle):
        buffer = ctypes.create_string_buffer(4097)
        read = wintypes.DWORD()
        state, token = self._lease._state, self._lease._operation
        if not self.k.SetFilePointerEx(handle, 0, None, 0):
            raise EpochStoreError("epoch held marker positioning failed")
        self._lease._current(state, token)
        if not self.k.ReadFile(handle, buffer, 4097, ctypes.byref(read), None):
            raise EpochStoreError("epoch held marker read failed")
        self._lease._current(state, token)
        if read.value > 4097:
            raise EpochStoreError("epoch held marker read exceeds bound")
        return buffer.raw[: read.value]

    def close(self, handle):
        if not self.k.CloseHandle(handle):
            raise EpochStoreError("epoch native cleanup outcome is unknown")


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

    def acquire(self, policy: TrustedEpochPolicy, receipt: EpochOpenAttemptReceipt) -> EpochOpenLease:
        # Deliberately no legacy probe, native call, fallback or lease factory.
        raise EpochStoreError("native epoch lifetime custody is unsupported")

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
