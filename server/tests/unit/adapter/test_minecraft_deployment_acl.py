import ctypes
import os
import struct

import pytest

from src.adapter.minecraft.deployment_acl import (
    _DANGEROUS,
    DeploymentAclError,
    HandleAce,
    HandleSecurityDescriptor,
    TrustedDeploymentAclPolicy,
    _AclSizeInformation,
    assess_descriptor,
    inspect_handle_security_descriptor,
)


def sid(rid: int) -> bytes:
    return b"\x01\x01" + (5).to_bytes(6, "big") + rid.to_bytes(4, "little")


OWNER = sid(100)
OTHER = sid(200)


@pytest.mark.parametrize(
    "status,pointer,throws", [(0, None, False), (5, None, False), (5, None, True), (0, 7, False), (5, 7, True)]
)
def test_owned_query_reentrant_close_is_sticky_unknown(status, pointer, throws):
    from src.adapter.minecraft.deployment_acl import DescriptorAllocationReceipt

    receipt = DescriptorAllocationReceipt()

    class ReentrantApi(Api):
        def get_security_info_owned(self, handle, owner, dacl, allocation):
            with pytest.raises(DeploymentAclError):
                receipt.close()
            allocation.value = pointer
            if throws:
                raise RuntimeError("query")
            return status

    api = ReentrantApi()
    with pytest.raises(DeploymentAclError, match="cleanup outcome is unknown"):
        inspect_handle_security_descriptor(1, allocation=receipt, _api=api)
    assert receipt.snapshot().state == "UNKNOWN"
    assert api.frees == 0


@pytest.mark.parametrize("failure,pointer", [(False, True), (True, False), (True, True)])
def test_owned_descriptor_query_and_opaque_failure_never_free_invalid_pointer(failure, pointer):
    from src.adapter.minecraft.deployment_acl import DescriptorAllocationReceipt

    receipt = DescriptorAllocationReceipt()

    class OwnedApi(Api):
        def get_security_info_owned(self, handle, owner, dacl, allocation):
            assert receipt.snapshot().state == "QUERYING"
            allocation.value = 1 if pointer else None
            owner.value = 1
            dacl.value = 2
            return 5 if failure else 0

    api = OwnedApi()
    if failure:
        with pytest.raises(DeploymentAclError):
            inspect_handle_security_descriptor(7, allocation=receipt, _api=api)
        assert receipt.snapshot().state == ("UNKNOWN" if pointer else "RELEASED")
        assert api.frees == 0
    else:
        inspect_handle_security_descriptor(7, allocation=receipt, _api=api)
        assert receipt.snapshot().state == "RELEASED"
        receipt.close()
        assert api.frees == 1


def test_owned_descriptor_free_unknown_and_first_fatal_identity():
    from src.adapter.minecraft.deployment_acl import DescriptorAllocationReceipt

    receipt = DescriptorAllocationReceipt()
    fatal = KeyboardInterrupt()

    class OwnedApi(Api):
        def get_security_info_owned(self, handle, owner, dacl, allocation):
            allocation.value = 1
            owner.value = 1
            dacl.value = 2
            return 0

        def get_dacl(self, descriptor):
            assert receipt.snapshot().state == "OWNED"
            raise fatal

    api = OwnedApi(free_error=SystemExit())
    with pytest.raises(KeyboardInterrupt) as caught:
        inspect_handle_security_descriptor(7, allocation=receipt, _api=api)
    assert caught.value is fatal
    assert receipt.snapshot().state == "UNKNOWN"
    with pytest.raises(DeploymentAclError):
        receipt.close()
    assert api.frees == 1


def test_owned_descriptor_throw_populated_is_opaque_and_reentry_free_once():
    from src.adapter.minecraft.deployment_acl import DescriptorAllocationReceipt

    receipt = DescriptorAllocationReceipt()
    fatal = KeyboardInterrupt()

    class ThrowApi(Api):
        def get_security_info_owned(self, handle, owner, dacl, allocation):
            allocation.value = 99
            raise fatal

    api = ThrowApi()
    with pytest.raises(KeyboardInterrupt) as caught:
        inspect_handle_security_descriptor(1, allocation=receipt, _api=api)
    assert caught.value is fatal and receipt.snapshot().state == "UNKNOWN" and api.frees == 0
    with pytest.raises(DeploymentAclError):
        receipt.close()
    assert api.frees == 0

    receipt = DescriptorAllocationReceipt()

    class ReenterApi(Api):
        def get_security_info_owned(self, handle, owner, dacl, allocation):
            allocation.value = 1
            owner.value = 1
            dacl.value = 2
            return 0

        def local_free(self, pointer):
            self.frees += 1
            with pytest.raises(DeploymentAclError):
                receipt.close()

    api = ReenterApi()
    with pytest.raises(DeploymentAclError, match="cleanup outcome is unknown"):
        inspect_handle_security_descriptor(1, allocation=receipt, _api=api)
    assert api.frees == 1 and receipt.snapshot().state == "UNKNOWN"


def policy(*writers):
    return TrustedDeploymentAclPolicy(
        expected_owner_sid=OWNER,
        allowed_writer_sids=writers or (OWNER,),
    )


def descriptor(*aces, owner=OWNER, protected=True):
    return HandleSecurityDescriptor(owner, protected, tuple(aces))


def allow(mask, who=OWNER, flags=0):
    return HandleAce(0, flags, mask, who)


def deny(mask, who=OTHER, flags=0):
    return HandleAce(1, flags, mask, who)


def test_pure_policy_accepts_protected_owner_and_read_only_other_sid():
    result = assess_descriptor(descriptor(allow(0x80000000, OTHER), allow(_DANGEROUS, OWNER)), policy())
    assert result.accepted


@pytest.mark.parametrize("mask", [1 << bit for bit in range(32) if _DANGEROUS & (1 << bit)])
def test_every_dangerous_bit_for_untrusted_or_inherited_allow_is_rejected(mask):
    assert not assess_descriptor(descriptor(allow(mask, OTHER)), policy()).accepted
    assert not assess_descriptor(descriptor(allow(mask, OWNER, 0x10)), policy()).accepted


def test_wrong_owner_unprotected_unknown_ace_flags_and_malformed_sid_reject():
    assert not assess_descriptor(descriptor(owner=OTHER), policy()).accepted
    assert not assess_descriptor(descriptor(protected=False), policy()).accepted
    assert not assess_descriptor(descriptor(HandleAce(2, 0, 0, OWNER)), policy()).accepted
    assert not assess_descriptor(descriptor(allow(0, OWNER, 0x80)), policy()).accepted
    for malformed in (b"", b"\x02\x01" + b"\0" * 10, b"\x01\x02" + b"\0" * 10):
        with pytest.raises(DeploymentAclError):
            TrustedDeploymentAclPolicy(expected_owner_sid=malformed, allowed_writer_sids=(malformed,))


def test_deny_ace_does_not_cancel_unsafe_allow_and_policy_writers_are_explicit_unique():
    result = assess_descriptor(descriptor(deny(_DANGEROUS, OTHER), allow(_DANGEROUS, OTHER)), policy())
    assert not result.accepted
    with pytest.raises(DeploymentAclError):
        TrustedDeploymentAclPolicy(expected_owner_sid=OWNER, allowed_writer_sids=())
    with pytest.raises(DeploymentAclError):
        TrustedDeploymentAclPolicy(expected_owner_sid=OWNER, allowed_writer_sids=(OWNER, OWNER))
    with pytest.raises(DeploymentAclError):
        TrustedDeploymentAclPolicy(expected_owner_sid=OWNER, allowed_writer_sids=(OTHER,))


class Api:
    def __init__(
        self, *, owner=OWNER, present=True, dacl=2, control=0x1000, aces=(), free_error=None, query_error=None
    ):
        self.owner = owner
        self.present = present
        self.dacl = dacl
        self.control = control
        self.ace_values = tuple(aces)
        self.free_error = free_error
        self.query_error = query_error
        self.frees = 0

    def get_security_info(self, _handle):
        if self.query_error is not None:
            raise self.query_error
        return 1, 3 if self.owner else 0, self.dacl

    def get_dacl(self, _descriptor):
        return self.present, self.dacl

    def get_control(self, _descriptor):
        return self.control

    def sid_bytes(self, _sid):
        return self.owner

    def aces(self, _dacl):
        return self.ace_values

    def local_free(self, _descriptor):
        self.frees += 1
        if self.free_error is not None:
            raise self.free_error


def test_descriptor_api_frees_once_on_success_error_and_rejects_absent_null_dacl():
    api = Api(aces=(allow(0),))
    value = inspect_handle_security_descriptor(123, _api=api)
    assert value.owner_sid == OWNER and api.frees == 1
    for api in (Api(owner=b""), Api(present=False), Api(dacl=0), Api(query_error=RuntimeError("raw"))):
        with pytest.raises(DeploymentAclError):
            inspect_handle_security_descriptor(123, _api=api)
        assert api.frees in (0, 1)


def test_cleanup_failure_and_fatal_error_are_honest_and_free_attempted_once():
    api = Api(free_error=RuntimeError("raw"))
    with pytest.raises(DeploymentAclError, match="cleanup outcome is unknown"):
        inspect_handle_security_descriptor(123, _api=api)
    assert api.frees == 1
    fatal = Api(query_error=RuntimeError("ordinary"), free_error=KeyboardInterrupt())
    # Query failed before descriptor ownership, so no LocalFree is attempted.
    with pytest.raises(DeploymentAclError):
        inspect_handle_security_descriptor(123, _api=fatal)

    owned_then_fatal = Api(owner=b"bad", free_error=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        inspect_handle_security_descriptor(123, _api=owned_then_fatal)
    assert owned_then_fatal.frees == 1


def test_public_errors_and_repr_are_redacted():
    value = descriptor(allow(0, OTHER))
    rendered = repr(value)
    assert OWNER.hex() not in rendered and OTHER.hex() not in rendered
    with pytest.raises(DeploymentAclError) as error:
        inspect_handle_security_descriptor(123, _api=Api(query_error=RuntimeError("secret native detail")))
    assert "secret" not in str(error.value)


class FakeAclAdvapi:
    def __init__(self, acl_address, acl_size, ace_address, ace_count=1):
        self.acl_address = acl_address
        self.acl_size = acl_size
        self.ace_address = ace_address
        self.ace_count = ace_count
        self.sid_calls = 0

    def GetAclInformation(self, _dacl, info, _size, _kind):
        value = ctypes.cast(info, ctypes.POINTER(_AclSizeInformation)).contents
        value.AceCount = self.ace_count
        value.AclBytesInUse = self.acl_size
        value.AclBytesFree = 0
        return 1

    def GetAce(self, _dacl, _index, pointer):
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p)).contents.value = self.ace_address
        return 1

    def IsValidSid(self, _sid):
        self.sid_calls += 1
        raise AssertionError("ACE interior SID pointer must never be passed to IsValidSid")

    def GetLengthSid(self, _sid):
        self.sid_calls += 1
        raise AssertionError("ACE interior SID pointer must never be passed to GetLengthSid")


def _acl_api(monkeypatch, ace_bytes, *, pointer_offset=8, acl_size=None):
    import src.adapter.minecraft.deployment_acl as module

    acl_size = acl_size or 8 + len(ace_bytes)
    buffer = ctypes.create_string_buffer(max(acl_size, 8 + len(ace_bytes)))
    ctypes.memmove(ctypes.addressof(buffer) + 8, ace_bytes, len(ace_bytes))
    fake = FakeAclAdvapi(ctypes.addressof(buffer), acl_size, ctypes.addressof(buffer) + pointer_offset)
    monkeypatch.setattr(module, "_advapi32", lambda: fake)
    return module._WindowsSecurityApi(), buffer, fake


def _raw_ace(sid_value=OWNER, *, declared_size=None):
    size = declared_size or 8 + len(sid_value)
    return struct.pack("<BBHI", 0, 0, size, 0) + sid_value


@pytest.mark.parametrize("offset", (-1, 1, 7))
def test_ace_pointer_before_acl_payload_or_short_header_rejects_without_dereference(monkeypatch, offset):
    api, buffer, fake = _acl_api(monkeypatch, _raw_ace(), pointer_offset=offset)
    with pytest.raises(DeploymentAclError):
        api.aces(ctypes.addressof(buffer))
    assert fake.sid_calls == 0


def test_ace_pointer_near_end_and_declared_size_beyond_acl_reject_without_sid_pointer_calls(monkeypatch):
    raw = _raw_ace()
    api, buffer, fake = _acl_api(monkeypatch, raw, pointer_offset=8 + len(raw) - 4)
    with pytest.raises(DeploymentAclError):
        api.aces(ctypes.addressof(buffer))
    assert fake.sid_calls == 0
    api, buffer, fake = _acl_api(monkeypatch, _raw_ace(declared_size=200), acl_size=8 + len(raw))
    with pytest.raises(DeploymentAclError):
        api.aces(ctypes.addressof(buffer))
    assert fake.sid_calls == 0


def test_copied_ace_requires_exact_canonical_sid_without_overrun_or_padding(monkeypatch):
    for invalid in (
        b"\x01\x02" + (5).to_bytes(6, "big") + (1).to_bytes(4, "little"),
        OWNER + b"padding",
    ):
        raw = _raw_ace(invalid)
        api, buffer, fake = _acl_api(monkeypatch, raw)
        with pytest.raises(DeploymentAclError):
            api.aces(ctypes.addressof(buffer))
        assert fake.sid_calls == 0


def test_exact_boundary_ace_parses_from_copied_bytes_without_native_sid_pointer_calls(monkeypatch):
    raw = _raw_ace()
    api, buffer, fake = _acl_api(monkeypatch, raw)
    assert api.aces(ctypes.addressof(buffer)) == (HandleAce(0, 0, 0, OWNER),)
    assert fake.sid_calls == 0


@pytest.mark.skipif(os.name != "nt", reason="requires actual Win32 GetSecurityInfo")
def test_real_windows_tempfile_handle_descriptor_is_read_and_freed(tmp_path):
    path = tmp_path / "acl-test.bin"
    path.write_bytes(b"x")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = ctypes.c_void_p
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x80, None)
    assert handle not in (None, ctypes.c_void_p(-1).value)
    try:
        value = inspect_handle_security_descriptor(handle)
        assert value.owner_sid and isinstance(value.aces, tuple)
        assessment = assess_descriptor(
            value,
            TrustedDeploymentAclPolicy(
                expected_owner_sid=value.owner_sid,
                allowed_writer_sids=(value.owner_sid,),
            ),
        )
        assert not assessment.accepted or value.dacl_protected
    finally:
        assert kernel.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="requires actual Win32 SetSecurityInfo")
def test_real_windows_protected_owner_only_dacl_is_accepted_on_unique_tempfile(tmp_path):
    path = tmp_path / "protected-acl-test.bin"
    path.write_bytes(b"x")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel.CreateFileW(str(path), 0x80000000 | 0x40000, 1, None, 3, 0x80, None)
    assert handle not in (None, ctypes.c_void_p(-1).value)
    try:
        original = inspect_handle_security_descriptor(handle)
        owner = original.owner_sid
        ace_size = 8 + len(owner)
        acl_size = 8 + ace_size
        acl = ctypes.create_string_buffer(
            struct.pack("<BBHHH", 2, 0, acl_size, 1, 0)
            + struct.pack("<BBHI", 0, 0, ace_size, _DANGEROUS | 0x80000000)
            + owner
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
        result = advapi.SetSecurityInfo(
            handle,
            1,
            0x4 | 0x80000000,
            None,
            None,
            ctypes.cast(acl, ctypes.c_void_p),
            None,
        )
        assert result == 0
        current = inspect_handle_security_descriptor(handle)
        assessment = assess_descriptor(
            current,
            TrustedDeploymentAclPolicy(
                expected_owner_sid=owner,
                allowed_writer_sids=(owner,),
            ),
        )
        assert current.dacl_protected and assessment.accepted
    finally:
        assert kernel.CloseHandle(handle)
