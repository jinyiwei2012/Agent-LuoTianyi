import os
import threading

import pytest

from src.adapter.minecraft import epoch_platform
from src.adapter.minecraft.epoch_platform import WindowsEpochPlatform, WindowsLockHandle
from src.adapter.minecraft.epoch_store import EpochStoreError


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
