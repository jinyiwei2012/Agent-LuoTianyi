import os
import threading
from dataclasses import replace
from unittest.mock import Mock

import pytest

from src.adapter.minecraft import epoch_platform
from src.adapter.minecraft.epoch_platform import WindowsEpochPlatform, WindowsLockHandle
from src.adapter.minecraft.epoch_store import EpochStoreError


def _native_policy():
    from src.adapter.minecraft.epoch_store import TrustedEpochPolicy

    sid = b"\x01\x01\x00\x00\x00\x00\x00\x05\x12\x00\x00\x00"
    return TrustedEpochPolicy(
        r"C:\test",
        1,
        2,
        sid,
        (sid,),
        (sid,),
        (sid,),
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000002",
    )


def _actual_fixture_owner():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parents[2] / "integration/adapter/test_minecraft_epoch_custody_native.py"
    spec = importlib.util.spec_from_file_location("native_fixture_resource_records", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.NativeFixtureOwner()


def test_fixture_ancestry_cleanup_unknown_is_original_failure_not_skip():
    owner = _actual_fixture_owner()
    failure = EpochStoreError("handle cleanup outcome is unknown")
    released = []

    def close(handle):
        released.append(handle)
        raise failure

    def assessment():
        with owner.resource("HANDLE", lambda: 123, close):
            pass  # Assessment itself succeeded; only release failed.

    with pytest.raises(EpochStoreError) as caught:
        owner.assess_ancestry(assessment)
    assert caught.value is failure and owner.unknown and released == [123]
    assert owner._records[0].state == "UNKNOWN"


def test_fixture_ancestry_unsupported_confirmed_release_can_skip():
    owner = _actual_fixture_owner()
    released = []

    def assessment():
        with owner.resource("HANDLE", lambda: 123, released.append):
            raise EpochStoreError("unsupported descriptor")

    with pytest.raises(pytest.skip.Exception, match="UNSUPPORTED_EXISTING_ANCESTRY"):
        owner.assess_ancestry(assessment)
    assert not owner.unknown and released == [123]
    assert owner._records[0].state == "RELEASED"


@pytest.mark.parametrize(
    "body,cleanup,winner",
    [
        (KeyboardInterrupt(), RuntimeError(), "body"),
        (KeyboardInterrupt(), SystemExit(), "body"),
        (RuntimeError(), SystemExit(), "cleanup"),
    ],
)
def test_fixture_store_body_cleanup_priority_retains_owner(body, cleanup, winner):
    import gc
    import weakref

    owner = _actual_fixture_owner()

    class Store:
        def __init__(self):
            self.closes = 0

        def close(self):
            self.closes += 1
            raise cleanup

    store = owner.retain(Store())
    reference = weakref.ref(store)
    try:
        with owner.store_lifetime(store):
            raise body
    except BaseException as caught:
        assert caught is (body if winner == "body" else cleanup)
        caught.__traceback__ = None
    else:
        pytest.fail("expected failure")
    body.__traceback__ = cleanup.__traceback__ = None
    del store
    gc.collect()
    assert reference() is not None and reference().closes == 1 and owner.unknown
    removed = Mock()
    with pytest.raises(RuntimeError):
        owner.cleanup_directory("retained", removed)
    removed.assert_not_called()


def test_fixture_store_normal_close_removes_only_confirmed_owner():
    owner = _actual_fixture_owner()
    store = Mock()
    owner.retain(store)
    with owner.store_lifetime(store):
        pass
    store.close.assert_called_once()
    assert all(value is None for value in owner._slots) and not owner.unknown


@pytest.mark.parametrize("kind", ["HANDLE", "SQLITE", "LOCAL_ALLOC"])
def test_actual_fixture_record_unknown_strong_owner_once_after_gc(kind):
    import gc
    import weakref

    owner = _actual_fixture_owner()
    calls = []

    class Opaque:
        pass

    def release(value):
        calls.append(value)
        raise RuntimeError("cleanup")

    if kind == "LOCAL_ALLOC":

        def query(output):
            output.value = 123
            return True

        record = owner.convert(query, release)
        target = record.output
    else:
        target = Opaque()
        record = owner.acquire(kind, lambda value=target: value, release)
    reference = weakref.ref(target)
    try:
        owner.close_records(record)
    except RuntimeError as failure:
        failure.__traceback__ = None
    calls.clear()
    del target
    gc.collect()
    assert reference() is not None and record.state == "UNKNOWN"
    with pytest.raises(RuntimeError):
        owner.close_records(record)
    assert calls == []
    removed = Mock()
    with pytest.raises(RuntimeError):
        owner.cleanup_directory("test", removed)
    removed.assert_not_called()


def test_actual_fixture_records_independent_cleanup_first_fatal_and_normal_release():
    owner = _actual_fixture_owner()
    calls = []
    first = KeyboardInterrupt()

    def fail(value):
        calls.append(value)
        raise first

    one = owner.acquire("HANDLE", lambda: "one", fail)
    two = owner.acquire("SQLITE", lambda: "two", lambda value: calls.append(value))
    with pytest.raises(KeyboardInterrupt) as caught:
        owner.close_records(one, two)
    assert caught.value is first and calls == ["one", "two"]
    assert one.state == "UNKNOWN" and two.state == "RELEASED" and two.value is None
    owner = _actual_fixture_owner()
    record = owner.acquire("HANDLE", lambda: "ok", lambda value: None)

    def reenter(value):
        with pytest.raises(RuntimeError):
            record.close()

    record.releaser = reenter
    with pytest.raises(RuntimeError):
        owner.close_records(record)
    assert record.state == "UNKNOWN" and record.close_attempted


@pytest.mark.parametrize("pointer,throws", [(None, False), (77, False), (None, True), (77, True)])
def test_actual_fixture_conversion_failed_output_table(pointer, throws):
    owner = _actual_fixture_owner()
    free = Mock()

    def query(output):
        output.value = pointer
        if throws:
            raise RuntimeError("query")
        return False

    with pytest.raises(RuntimeError):
        owner.convert(query, free)
    record = owner._records[0]
    assert record.state == ("UNKNOWN" if pointer else "RELEASED")
    assert owner.unknown == bool(pointer)
    free.assert_not_called()
    if pointer:
        with pytest.raises(RuntimeError):
            owner.close_records(record)
        free.assert_not_called()


def test_actual_fixture_reserves_capacity_before_acquisition_and_reuses_only_release():
    owner = _actual_fixture_owner()
    factory = Mock(return_value=object())
    records = [owner.acquire("HANDLE", factory, lambda value: None) for _ in range(8)]
    with pytest.raises(RuntimeError):
        owner.acquire("SQLITE", factory, lambda value: None)
    assert factory.call_count == 8
    owner.close_records(records[0])
    replacement = owner.acquire("HANDLE", factory, lambda value: None)
    assert replacement is not records[0] and factory.call_count == 9


def test_actual_fixture_conversion_reentry_unknown_is_monotonic_and_success_free_once():
    owner = _actual_fixture_owner()
    free = Mock()

    def query(output):
        with pytest.raises(RuntimeError):
            owner._records[0].close()
        raise RuntimeError("query failed after reentry")

    with pytest.raises(RuntimeError):
        owner.convert(query, free)
    assert owner._records[0].state == "UNKNOWN" and owner.unknown
    free.assert_not_called()
    owner = _actual_fixture_owner()

    def success(output):
        output.value = 99
        return True

    record = owner.convert(success, free)
    owner.close_records(record)
    owner.close_records(record)
    free.assert_called_once_with(99)
    assert record.output.value is None and record.value is None and record.state == "RELEASED"


def test_actual_native_fixture_session_owner_survives_traceback_clear_gc():
    import gc
    import importlib.util
    import weakref
    from pathlib import Path

    path = Path(__file__).parents[2] / "integration/adapter/test_minecraft_epoch_custody_native.py"
    spec = importlib.util.spec_from_file_location("native_fixture_ownership_only", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    owner = module.NativeFixtureOwner()
    references = []

    def bootstrap_failure():
        resource = owner.retain(epoch_platform._NativeEpochLease(None, None, []))
        references.append(weakref.ref(resource))
        owner.mark_unknown()
        raise RuntimeError("token bootstrap failed")

    try:
        bootstrap_failure()
    except RuntimeError as failure:
        failure.__traceback__ = None
    gc.collect()
    assert references[0]() is not None and owner.unknown
    receipt = owner.retain(epoch_platform.DescriptorAllocationReceipt())
    reference = weakref.ref(receipt)
    del receipt
    gc.collect()
    assert reference() is not None


def test_actual_marker_position_reentry_stops_before_native_read():
    lease = epoch_platform._NativeEpochLease(None, None, [])
    lease._state = "ACTIVE"
    lease._operation = object()
    api = object.__new__(epoch_platform._EpochNativeApi)
    api._lease = lease
    api.k = Mock()

    def position(*args):
        with pytest.raises(EpochStoreError):
            lease.close()
        return 1

    api.k.SetFilePointerEx.side_effect = position
    with pytest.raises(EpochStoreError):
        api.read_marker(123)
    api.k.ReadFile.assert_not_called()
    assert lease._state == "UNKNOWN"


@pytest.mark.parametrize(
    "raw,count",
    [
        ("\\Device\\HarddiskVolume1\0", None),
        ("\\Device\\HarddiskVolume1\0extra\0\0", None),
        ("\\Device\\HarddiskVolume1suffix\0\0", None),
        ("", 1024),
        ("", 0),
    ],
)
def test_actual_mapping_rejects_malformed_multisz(raw, count):
    api = object.__new__(epoch_platform._EpochNativeApi)
    api.k = Mock()

    def query(drive, buffer, capacity):
        for index, char in enumerate(raw):
            buffer[index] = char
        return len(raw) if count is None else count

    api.k.QueryDosDeviceW.side_effect = query
    with pytest.raises(EpochStoreError):
        api.mapping("C:\\")
    api.k.GetDriveTypeW.assert_not_called()


@pytest.mark.parametrize(
    "size,raw,after,success",
    [(4096, b"x" * 4096, 4096, True), (4097, b"x" * 4097, 4097, False), (3, b"xx", 3, False), (3, b"xxx", 4, False)],
)
def test_marker_bounded_sizes_and_permanent_invalidity(size, raw, after, success):
    lease = epoch_platform._NativeEpochLease(None, None, ["marker", "db"])
    lease._state = "ACTIVE"
    lease._handles = [1, 2]
    lease.validate = Mock()
    lease._api = Mock()
    lease._api.size.side_effect = [size, after]
    lease._api.read_marker.return_value = raw
    if success:
        assert lease.read_marker() == raw
    else:
        with pytest.raises(EpochStoreError):
            lease.read_marker()
        assert lease._state == "INVALID"
    if size > 4096:
        lease._api.read_marker.assert_not_called()


def test_combined_temporary_failures_preserve_first_fatal_and_close_files():
    from src.adapter.minecraft.deployment_acl import DeploymentAclError

    lease = epoch_platform._NativeEpochLease(None, None, ["a", "b"])
    lease._state = "INVALID"
    lease._handles = [11, 12]
    first = KeyboardInterrupt()
    later = SystemExit()
    token = epoch_platform._TokenAllocation()
    token.output.value = 44
    token.establish(True)
    lease._tokens[0] = token
    receipt = Mock()
    receipt.close.side_effect = later
    lease._descriptor_allocation = receipt
    api = Mock()

    def close(handle):
        if handle == 44:
            raise first
        if handle == 12:
            raise DeploymentAclError("cleanup outcome is unknown")

    api.close.side_effect = close
    lease._api = api
    with pytest.raises(KeyboardInterrupt) as caught:
        lease.close()
    assert caught.value is first
    assert [args.args[0] for args in api.close.call_args_list] == [44, 12, 11]
    with pytest.raises(EpochStoreError):
        lease.close()
    assert api.close.call_count == 3


@pytest.mark.parametrize("query_ok,close_unknown", [(False, False), (False, True), (True, True), (True, False)])
def test_actual_token_query_cleanup_table(monkeypatch, query_ok, close_unknown):
    lease = epoch_platform._NativeEpochLease(None, None, [])
    lease._state = "ACTIVE"
    lease._operation = object()
    api = object.__new__(epoch_platform._EpochNativeApi)
    api._lease = lease
    api.k = Mock()
    native = Mock()

    def open_token(thread, access, impersonate, output):
        output._obj.value = 22
        return 1

    native.OpenThreadToken.side_effect = open_token
    native.GetTokenInformation.return_value = int(query_ok)
    monkeypatch.setattr(epoch_platform.ctypes, "WinDLL", lambda *args, **kwargs: native)
    monkeypatch.setattr(api, "_owner_bytes", lambda *args: _native_policy().expected_owner_sid)
    api.close = Mock(side_effect=EpochStoreError("cleanup outcome is unknown") if close_unknown else None)
    if not query_ok or close_unknown:
        with pytest.raises(EpochStoreError):
            api.token_owner()
    else:
        assert api.token_owner() == _native_policy().expected_owner_sid
    api.close.assert_called_once_with(22)
    assert lease._tokens[0].state == ("UNKNOWN" if close_unknown else "RELEASED")
    if close_unknown:
        with pytest.raises(EpochStoreError):
            lease._tokens[0].close(api)
        api.close.assert_called_once()


def test_actual_token_open_throw_populated_is_opaque(monkeypatch):
    lease = epoch_platform._NativeEpochLease(None, None, [])
    lease._state = "ACTIVE"
    lease._operation = object()
    api = object.__new__(epoch_platform._EpochNativeApi)
    api._lease = lease
    api.k = Mock()
    native = Mock()
    fatal = KeyboardInterrupt()

    def open_token(thread, access, impersonate, output):
        output._obj.value = 55
        raise fatal

    native.OpenThreadToken.side_effect = open_token
    with pytest.raises(KeyboardInterrupt) as caught:
        api._open_token(native)
    assert caught.value is fatal and lease._tokens[0].state == "OPAQUE_UNKNOWN"
    native.OpenProcessToken.assert_not_called()
    native.GetTokenInformation.assert_not_called()
    with pytest.raises(EpochStoreError):
        lease._tokens[0].close(api)
    api.k.CloseHandle.assert_not_called()


def test_mapping_baseline_change_invalidates_before_any_identity_query():
    lease = epoch_platform._NativeEpochLease(None, None, ["C:\\"])
    lease._state = "ACTIVE"
    lease._mapping = "old"
    lease._api = Mock()
    lease._api.mapping.return_value = "new"
    with pytest.raises(EpochStoreError):
        lease.validate()
    lease._api.info.assert_not_called()
    assert lease._state == "INVALID"
    with pytest.raises(EpochStoreError):
        lease.validate()
    assert lease._api.mapping.call_count == 1


@pytest.mark.parametrize("success,value", [(False, None), (False, 99), (True, None), (True, 99)])
def test_token_owned_output_table(success, value):
    allocation = epoch_platform._TokenAllocation()
    allocation.output.value = value
    api = Mock()
    if success and value is None:
        with pytest.raises(EpochStoreError):
            allocation.establish(success)
    else:
        assert allocation.establish(success) == bool(success and value)
    if allocation.state in {"OPAQUE_UNKNOWN", "UNKNOWN"}:
        with pytest.raises(EpochStoreError):
            allocation.close(api)
        api.close.assert_not_called()
    else:
        allocation.close(api)
        assert api.close.call_count == int(success and value is not None)


@pytest.mark.parametrize("failure,value,error", [(False, 8, 0), (True, None, 1008), (True, 8, 1008), (True, None, 5)])
def test_actual_token_open_fallback_only_null_no_token(monkeypatch, failure, value, error):
    policy = _native_policy()
    lease = epoch_platform._NativeEpochLease(policy, None, [])
    lease._state = "ACTIVE"
    lease._operation = object()
    api = object.__new__(epoch_platform._EpochNativeApi)
    api._lease = lease
    api.k = Mock()
    native = Mock()

    def thread(handle, access, impersonate, output):
        output._obj.value = value
        return not failure

    def process(handle, access, output):
        output._obj.value = 9
        return True

    native.OpenThreadToken.side_effect = thread
    native.OpenProcessToken.side_effect = process
    monkeypatch.setattr(epoch_platform.ctypes, "get_last_error", lambda: error)
    if not failure or value is None and error == 1008:
        allocation = api._open_token(native)
        assert allocation.state == "OWNED"
        assert native.OpenProcessToken.call_count == int(failure)
    else:
        with pytest.raises(EpochStoreError):
            api._open_token(native)
        native.OpenProcessToken.assert_not_called()
        if value:
            assert lease._tokens[0].state == "OPAQUE_UNKNOWN"
            with pytest.raises(EpochStoreError):
                lease._tokens[0].close(api)
            api.k.CloseHandle.assert_not_called()


def test_token_throw_populated_and_close_reentry_never_retry():
    allocation = epoch_platform._TokenAllocation()
    allocation.output.value = 77
    allocation.establish(False)
    api = Mock()
    with pytest.raises(EpochStoreError):
        allocation.close(api)
    api.close.assert_not_called()
    allocation = epoch_platform._TokenAllocation()
    allocation.output.value = 88
    allocation.establish(True)

    def close(handle):
        with pytest.raises(EpochStoreError):
            allocation.close(api)

    api.close.side_effect = close
    with pytest.raises(EpochStoreError):
        allocation.close(api)
    assert api.close.call_count == 1


@pytest.mark.parametrize("boundary", ["mapping", "info", "descriptor", "token_owner", "size", "read_marker"])
def test_composite_reentrant_close_cannot_continue_or_launder_unknown(boundary):  # noqa: C901 - finite boundary matrix
    policy = _native_policy()
    profile = epoch_platform.NativeEpochOpenProfile(policy.expected_owner_sid, "EXPLICIT_INHERITABLE_ALLOW_V1")
    lease = epoch_platform._NativeEpochLease(policy, profile, epoch_platform._native_paths(policy))
    lease._state = "ACTIVE"
    lease._mapping = "mapping"
    lease._handles = [1] * len(lease._handles)
    calls = []

    class Api:
        def mapping(self, root):
            calls.append("mapping")
            if boundary == "mapping":
                with pytest.raises(EpochStoreError):
                    lease.close()
            return "mapping"

        def info(self, handle):
            calls.append("info")
            if boundary == "info":
                with pytest.raises(EpochStoreError):
                    lease.close()
            return dict(volume=1, index=2, directory=True, reparse=False, delete_pending=False, links=1)

        def descriptor(self, handle, allocation):
            calls.append("descriptor")
            with pytest.raises(EpochStoreError):
                lease.close()
            raise EpochStoreError("stop descriptor boundary")

        def size(self, handle):
            calls.append("size")
            with pytest.raises(EpochStoreError):
                lease.close()
            return 1

        def read_marker(self, handle):
            calls.append("read_marker")
            with pytest.raises(EpochStoreError):
                lease.close()
            return b"x"

        def close(self, handle):
            calls.append("close")

    lease._api = Api()
    if boundary in {"size", "read_marker"}:
        lease.validate = lambda: None
        if boundary == "read_marker":
            lease._api.size = lambda handle: 1
        with pytest.raises(EpochStoreError):
            lease.read_marker()
        if boundary == "size":
            assert "read_marker" not in calls
    elif boundary == "token_owner":
        # root inspection's token callback must invalidate exact operation before return.
        from src.adapter.minecraft.deployment_acl import HandleAce, HandleSecurityDescriptor

        lease._api.descriptor = lambda handle, receipt: HandleSecurityDescriptor(
            policy.expected_owner_sid, True, (HandleAce(0, 3, 0x120116, policy.expected_owner_sid),)
        )

        def token_owner():
            with pytest.raises(EpochStoreError):
                lease.close()
            return policy.expected_owner_sid

        lease._api.token_owner = token_owner
        lease._operation = object()
        with pytest.raises(EpochStoreError):
            lease._inspect(len(lease._paths) - 4)
    else:
        with pytest.raises(EpochStoreError):
            lease.validate()
    assert lease._state == "UNKNOWN"
    assert "close" not in calls


def test_journal_prediction_exact_multiset_not_category_approximation():
    from src.adapter.minecraft.deployment_acl import HandleAce, HandleSecurityDescriptor

    policy = _native_policy()
    sid = policy.expected_owner_sid
    profile = epoch_platform.NativeEpochOpenProfile(sid, "EXPLICIT_INHERITABLE_ALLOW_V1")
    root = HandleSecurityDescriptor(sid, True, (HandleAce(0, 3, 1, sid), HandleAce(0, 3, 2, sid)))
    prediction = epoch_platform._journal_prediction(root, policy, profile, sid)
    actual = HandleSecurityDescriptor(sid, False, (HandleAce(0, 16, 2, sid), HandleAce(0, 16, 1, sid)))
    epoch_platform._match_journal(actual, prediction, policy)
    with pytest.raises(EpochStoreError):
        epoch_platform._match_journal(
            HandleSecurityDescriptor(sid, False, actual.aces + actual.aces[:1]), prediction, policy
        )


@pytest.mark.parametrize("mask", [0x40, 0x10000000, 0x1F01FF, 0x2000000])
def test_restricted_native_file_masks_reject_unsupported_bits(mask):
    with pytest.raises(EpochStoreError):
        epoch_platform._expanded(mask, False)


def test_restricted_native_acl_sets_and_journal_prediction():
    from src.adapter.minecraft.deployment_acl import HandleAce, HandleSecurityDescriptor

    policy = _native_policy()
    descriptor = HandleSecurityDescriptor(
        policy.expected_owner_sid, True, (HandleAce(0, 3, 0x120116, policy.expected_owner_sid),)
    )
    profile = epoch_platform.NativeEpochOpenProfile(policy.expected_owner_sid, "EXPLICIT_INHERITABLE_ALLOW_V1")
    epoch_platform._assess_epoch(descriptor, policy, True, True)
    epoch_platform._journal_prediction(descriptor, policy, profile, policy.expected_owner_sid)
    other = policy.expected_owner_sid[:-4] + b"\x13\0\0\0"
    with pytest.raises(EpochStoreError):
        epoch_platform._assess_epoch(descriptor, replace(policy, content_writer_sids=(other,)), True, True)
    for flags in (1, 2, 8, 16):
        with pytest.raises(EpochStoreError):
            epoch_platform._assess_aces((HandleAce(0, flags, 1, policy.expected_owner_sid),), policy, True)


def test_native_backend_partial_handle_registered_before_inspection(monkeypatch):
    from src.adapter.minecraft.epoch_store import EpochOpenAttemptReceipt, EpochStore

    policy = _native_policy()
    calls = []

    class Api:
        def __init__(self, lease):
            pass

        def mapping(self, root):
            calls.append("mapping")

        def open(self, path, kind):
            calls.append("open")
            return 123

        def info(self, handle):
            raise KeyboardInterrupt()

        def close(self, handle):
            calls.append(("close", handle))

    monkeypatch.setattr(epoch_platform, "_EpochNativeApi", Api)
    profile = epoch_platform.NativeEpochOpenProfile(policy.expected_owner_sid, "EXPLICIT_INHERITABLE_ALLOW_V1")
    backend = epoch_platform.WindowsEpochCustodyPlatform(profile)
    assert calls == []
    receipt = EpochOpenAttemptReceipt()
    with pytest.raises(KeyboardInterrupt):
        EpochStore.open_with_custody(policy, custody=backend, receipt=receipt)
    assert receipt.snapshot().state == "RELEASED"
    assert calls.count(("close", 123)) == 1


def test_custody_native_unsupported_has_zero_native_or_legacy_calls(monkeypatch):
    from src.adapter.minecraft.epoch_store import EpochOpenAttemptReceipt, EpochStore, TrustedEpochPolicy

    sid = b"\x01\x01\x00\x00\x00\x00\x00\x05\x12\x00\x00\x00"
    policy = TrustedEpochPolicy(
        r"C:\test",
        1,
        2,
        sid,
        (sid,),
        (sid,),
        (sid,),
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000002",
    )

    def forbidden(*args):
        raise AssertionError("native/legacy fallback")

    monkeypatch.setattr(epoch_platform, "_kernel32", forbidden)
    for name in ("capability", "volume_facts", "acquire_lock"):
        monkeypatch.setattr(WindowsEpochPlatform, name, forbidden)
    receipt = EpochOpenAttemptReceipt()
    with pytest.raises(EpochStoreError, match="unsupported"):
        EpochStore.open_with_custody(policy, custody=WindowsEpochPlatform(), receipt=receipt)
    assert receipt.snapshot().state == "RELEASED"


class _Kernel:
    def __init__(self, result=1, error=None):
        self.result = result
        self.error = error
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = False

    def CloseHandle(self, _handle):
        self.calls += 1
        self.entered.set()
        if self.block and not self.release.wait(timeout=2):
            raise RuntimeError("test release timeout")
        if self.error is not None:
            raise self.error
        return self.result


class _ReentrantKernel:
    def __init__(self):
        self.handle = None
        self.calls = 0
        self.nested_errors = []

    def CloseHandle(self, _handle):
        self.calls += 1
        try:
            self.handle.close()
        except EpochStoreError as error:
            self.nested_errors.append(str(error))
        return 1


def test_platform_is_fail_closed_without_acl_descriptor_verification(tmp_path):
    capability = WindowsEpochPlatform().capability(tmp_path)
    assert not capability.verified
    assert "ACL" in capability.reason or "local volume" in capability.reason


def test_non_windows_lock_is_unavailable(tmp_path):
    if os.name == "nt":
        pytest.skip("covered by Windows-only lifetime lock test")
    lock_path = tmp_path / "epochs.lock"
    lock_path.touch()
    with pytest.raises(EpochStoreError):
        WindowsLockHandle.acquire(lock_path)


def test_windows_lock_close_success_is_idempotent_and_clears_handle(monkeypatch):
    kernel = _Kernel(result=1)
    monkeypatch.setattr(epoch_platform, "_kernel32", lambda: kernel)
    handle = WindowsLockHandle(123)
    handle.close()
    handle.close()
    assert kernel.calls == 1
    assert handle._handle is None


@pytest.mark.parametrize("result,error", [(0, None), (None, RuntimeError("injected"))])
def test_windows_lock_close_failure_becomes_unknown_without_retry(monkeypatch, result, error):
    kernel = _Kernel(result=result, error=error)
    monkeypatch.setattr(epoch_platform, "_kernel32", lambda: kernel)
    handle = WindowsLockHandle(123)
    with pytest.raises(EpochStoreError, match="outcome is unknown"):
        handle.close()
    with pytest.raises(EpochStoreError, match="outcome is unknown"):
        handle.close()
    assert kernel.calls == 1
    assert handle._handle == 123


def test_windows_lock_concurrent_close_makes_one_native_attempt(monkeypatch):
    kernel = _Kernel(result=1)
    kernel.block = True
    monkeypatch.setattr(epoch_platform, "_kernel32", lambda: kernel)
    handle = WindowsLockHandle(123)
    errors = []

    def close():
        try:
            handle.close()
        except Exception as error:  # pragma: no cover - assertion records unexpected errors
            errors.append(error)

    first = threading.Thread(target=close)
    second = threading.Thread(target=close)
    first.start()
    assert kernel.entered.wait(timeout=2)
    second.start()
    kernel.release.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert kernel.calls == 1


@pytest.mark.parametrize("fatal", [KeyboardInterrupt(), SystemExit()])
def test_windows_lock_fatal_close_exit_becomes_unknown_without_retry(monkeypatch, fatal):
    kernel = _Kernel(error=fatal)
    monkeypatch.setattr(epoch_platform, "_kernel32", lambda: kernel)
    handle = WindowsLockHandle(123)
    caught = None
    try:
        handle.close()
    except BaseException as error:
        caught = error
    assert type(caught) is type(fatal)
    with pytest.raises(EpochStoreError, match="outcome is unknown"):
        handle.close()
    assert kernel.calls == 1
    assert handle._handle == 123


def test_windows_lock_reentrant_close_cannot_revive_unknown_state(monkeypatch):
    kernel = _ReentrantKernel()
    monkeypatch.setattr(epoch_platform, "_kernel32", lambda: kernel)
    handle = WindowsLockHandle(123)
    kernel.handle = handle
    with pytest.raises(EpochStoreError, match="outcome is unknown"):
        handle.close()
    with pytest.raises(EpochStoreError, match="outcome is unknown"):
        handle.close()
    assert kernel.calls == 1
    assert kernel.nested_errors == ["Windows epoch lock cleanup outcome is unknown"]
    assert handle._handle == 123


def test_windows_lock_concurrent_failed_close_attempts_native_once(monkeypatch):
    kernel = _Kernel(result=0)
    kernel.block = True
    monkeypatch.setattr(epoch_platform, "_kernel32", lambda: kernel)
    handle = WindowsLockHandle(123)
    errors = []

    def close():
        try:
            handle.close()
        except EpochStoreError as error:
            errors.append(str(error))

    first = threading.Thread(target=close)
    second = threading.Thread(target=close)
    first.start()
    assert kernel.entered.wait(timeout=2)
    second.start()
    kernel.release.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive() and not second.is_alive()
    assert errors == [
        "Windows epoch lock cleanup outcome is unknown",
        "Windows epoch lock cleanup outcome is unknown",
    ]
    assert kernel.calls == 1


def test_windows_invalid_handle_error_is_unknown_not_success(monkeypatch):
    kernel = _Kernel(result=0)
    monkeypatch.setattr(epoch_platform, "_kernel32", lambda: kernel)
    monkeypatch.setattr(epoch_platform.ctypes, "get_last_error", lambda: 6)
    handle = WindowsLockHandle(123)
    with pytest.raises(EpochStoreError, match="outcome is unknown"):
        handle.close()
    assert kernel.calls == 1
    assert handle._handle == 123


@pytest.mark.skipif(os.name != "nt", reason="requires real Win32 CreateFileW")
def test_windows_no_share_lock_rejects_second_handle_and_releases(tmp_path):
    lock_path = tmp_path / "epochs.lock"
    lock_path.touch()
    first = WindowsLockHandle.acquire(lock_path)
    try:
        with pytest.raises(EpochStoreError):
            WindowsLockHandle.acquire(lock_path)
    finally:
        first.close()
    second = WindowsLockHandle.acquire(lock_path)
    second.close()
    second.close()
