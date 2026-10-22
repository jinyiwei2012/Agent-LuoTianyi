import os

import pytest

from src.adapter.minecraft.epoch_platform import WindowsEpochPlatform, WindowsLockHandle
from src.adapter.minecraft.epoch_store import EpochStoreError


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
