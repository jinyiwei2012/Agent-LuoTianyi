import json
import sqlite3
import threading
from pathlib import Path

import pytest

from src.adapter.minecraft.epoch_store import (
    EpochCandidate,
    EpochStore,
    EpochStoreError,
    LockHandle,
    PlatformCapability,
    VerifiedReadyContext,
    _connect_rw,
)

PROVISION = "00000000-0000-4000-8000-000000000001"
BODY = "00000000-0000-4000-8000-000000000002"


class _Lock:
    def __init__(self, locks: set[Path], path: Path):
        self._locks, self._path = locks, path

    def close(self) -> None:
        self._locks.discard(self._path)


class _CountingLock(_Lock):
    def __init__(self, locks: set[Path], path: Path):
        super().__init__(locks, path)
        self.closes = 0

    def close(self) -> None:
        self.closes += 1
        super().close()


class _FailBeforeReleaseLock(_CountingLock):
    def close(self) -> None:
        self.closes += 1
        raise EpochStoreError("injected close failure")


class _ReleaseThenFailLock(_CountingLock):
    def close(self) -> None:
        self.closes += 1
        self._locks.discard(self._path)
        raise EpochStoreError("injected post-release failure")


class _FatalLock(_CountingLock):
    def __init__(self, locks: set[Path], path: Path, fatal=KeyboardInterrupt()):
        super().__init__(locks, path)
        self.fatal = fatal

    def close(self) -> None:
        self.closes += 1
        raise self.fatal


class _ReentrantLock(_CountingLock):
    def __init__(self, locks: set[Path], path: Path):
        super().__init__(locks, path)
        self.store = None
        self.nested_errors = []

    def close(self) -> None:
        self.closes += 1
        try:
            self.store.close()
        except EpochStoreError as error:
            self.nested_errors.append(str(error))
        self._locks.discard(self._path)


class _TestPlatform:
    """Test double only; it is not evidence of Windows ACL or VFS guarantees."""

    def __init__(self, verified: bool = True):
        self.verified = verified
        self.locks: set[Path] = set()

    def capability(self, directory: Path) -> PlatformCapability:
        return PlatformCapability(self.verified, "isolated test capability")

    def acquire_lock(self, lock_path: Path) -> LockHandle:
        if lock_path in self.locks:
            raise EpochStoreError("lock conflict")
        self.locks.add(lock_path)
        return _Lock(self.locks, lock_path)


class _CountingPlatform(_TestPlatform):
    def __init__(self, *, fail_lock: bool = False, lock_type=_CountingLock):
        super().__init__()
        self.fail_lock = fail_lock
        self.lock_type = lock_type
        self.handle: _CountingLock | None = None

    def acquire_lock(self, lock_path: Path) -> LockHandle:
        if self.fail_lock:
            raise EpochStoreError("injected lock failure")
        if lock_path in self.locks:
            raise EpochStoreError("lock conflict")
        self.locks.add(lock_path)
        self.handle = self.lock_type(self.locks, lock_path)
        return self.handle


class _DoubleFaultConnection:
    def __init__(self, connection):
        self._connection = connection
        self.closed = 0

    def execute(self, statement, *args):
        if statement == "COMMIT":
            raise sqlite3.OperationalError("injected commit failure")
        if statement == "ROLLBACK":
            raise sqlite3.OperationalError("injected rollback failure")
        return self._connection.execute(statement, *args)

    def close(self):
        self.closed += 1
        self._connection.close()


def ready(candidate):
    return VerifiedReadyContext(
        candidate.provision_id,
        candidate.body_id,
        candidate.expected_last_epoch,
        candidate.expected_generation,
        candidate.next_epoch,
        "trusted-registry-evidence",
    )


def test_default_platform_is_disabled_and_missing_open_creates_no_files(tmp_path):
    with pytest.raises(EpochStoreError):
        EpochStore.open(tmp_path / "missing")
    assert not (tmp_path / "missing").exists()
    with pytest.raises(EpochStoreError):
        EpochStore.provision(tmp_path / "unsupported", PROVISION, BODY)
    assert not (tmp_path / "unsupported").exists()


def test_provision_requires_empty_install_and_open_rejects_partial_states(tmp_path):
    port = _TestPlatform()
    store = EpochStore.provision(tmp_path / "epoch", PROVISION, BODY, platform=port)
    store.close()
    for name in ("epochs.sqlite3", "provision.json", "epochs.lock"):
        root = tmp_path / name
        root.mkdir()
        (root / name).touch()
        with pytest.raises(EpochStoreError):
            EpochStore.open(root, platform=port)


def test_candidate_does_not_consume_and_commit_persists_across_reopen(tmp_path):
    port = _TestPlatform()
    root = tmp_path / "epoch"
    store = EpochStore.provision(root, PROVISION, BODY, platform=port)
    candidate = store.read_candidate()
    assert store.read_candidate() == candidate
    committed = store.commit_ready(candidate, ready(candidate))
    assert (committed.last_epoch, committed.generation) == (1, 1)
    store.close()
    reopened = EpochStore.open(root, platform=port)
    assert reopened.read_candidate().next_epoch == 2
    reopened.close()


def test_commit_rejects_stale_or_untrusted_ready_and_disables_after_rejection(tmp_path):
    port = _TestPlatform()
    store = EpochStore.provision(tmp_path / "epoch", PROVISION, BODY, platform=port)
    candidate = store.read_candidate()
    wrong = VerifiedReadyContext(PROVISION, BODY, 0, 0, 1, "")
    with pytest.raises(EpochStoreError):
        store.commit_ready(candidate, wrong)
    with pytest.raises(EpochStoreError):
        store.read_candidate()


def test_open_rejects_marker_body_mismatch_and_database_corruption(tmp_path):
    port = _TestPlatform()
    root = tmp_path / "epoch"
    store = EpochStore.provision(root, PROVISION, BODY, platform=port)
    store.close()
    (root / "provision.json").write_text(
        '{"version":1,"provisionId":"' + PROVISION + '","bodyId":"00000000-0000-4000-8000-000000000003"}'
    )
    with pytest.raises(EpochStoreError):
        EpochStore.open(root, platform=port)


def test_lock_conflict_and_i63_exhaustion_fail_closed(tmp_path):
    port = _TestPlatform()
    root = tmp_path / "epoch"
    first = EpochStore.provision(root, PROVISION, BODY, platform=port)
    with pytest.raises(EpochStoreError):
        EpochStore.open(root, platform=port)
    first.close()
    reopened = EpochStore.open(root, platform=port)
    reopened.close()


def test_close_is_idempotent_and_rejects_reads_and_commits_without_database_change(tmp_path):
    port = _CountingPlatform()
    store = EpochStore.provision(tmp_path / "epoch", PROVISION, BODY, platform=port)
    candidate = store.read_candidate()
    store.close()
    store.close()
    assert port.handle is not None and port.handle.closes == 1
    with pytest.raises(EpochStoreError):
        store.read_candidate()
    with pytest.raises(EpochStoreError):
        store.commit_ready(candidate, ready(candidate))
    reopened = EpochStore.open(tmp_path / "epoch", platform=port)
    assert reopened.read_candidate().next_epoch == 1
    reopened.close()


@pytest.mark.parametrize("lock_type", [_FailBeforeReleaseLock, _ReleaseThenFailLock])
def test_close_failure_permanently_disables_operations_and_never_retries_uncertain_cleanup(tmp_path, lock_type):
    port = _CountingPlatform(lock_type=lock_type)
    store = EpochStore.provision(tmp_path / "epoch", PROVISION, BODY, platform=port)
    candidate = store.read_candidate()
    with pytest.raises(EpochStoreError, match="cleanup outcome is unknown"):
        store.close()
    with pytest.raises(EpochStoreError, match="disabled"):
        store.read_candidate()
    with pytest.raises(EpochStoreError, match="disabled"):
        store.commit_ready(candidate, ready(candidate))
    with pytest.raises(EpochStoreError, match="cleanup outcome is unknown"):
        store.close()
    assert port.handle is not None and port.handle.closes == 1


def test_concurrent_successful_close_is_serialized_and_attempted_once(tmp_path):
    port = _CountingPlatform()
    store = EpochStore.provision(tmp_path / "epoch", PROVISION, BODY, platform=port)
    errors = []

    def close():
        try:
            store.close()
        except Exception as error:  # pragma: no cover - assertion records unexpected errors
            errors.append(error)

    threads = [threading.Thread(target=close) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert port.handle is not None and port.handle.closes == 1


@pytest.mark.parametrize("fatal_type", [KeyboardInterrupt, SystemExit])
def test_store_fatal_close_exit_disables_operations_and_never_retries(tmp_path, fatal_type):
    class FatalTypeLock(_FatalLock):
        def __init__(self, locks, path):
            super().__init__(locks, path, fatal_type())

    port = _CountingPlatform(lock_type=FatalTypeLock)
    store = EpochStore.provision(tmp_path / "epoch", PROVISION, BODY, platform=port)
    candidate = store.read_candidate()
    caught = None
    try:
        store.close()
    except BaseException as error:
        caught = error
    assert type(caught) is fatal_type
    with pytest.raises(EpochStoreError, match="disabled"):
        store.read_candidate()
    with pytest.raises(EpochStoreError, match="disabled"):
        store.commit_ready(candidate, ready(candidate))
    with pytest.raises(EpochStoreError, match="cleanup outcome is unknown"):
        store.close()
    assert port.handle is not None and port.handle.closes == 1


def test_store_reentrant_close_cannot_revive_unknown_cleanup(tmp_path):
    port = _CountingPlatform(lock_type=_ReentrantLock)
    store = EpochStore.provision(tmp_path / "epoch", PROVISION, BODY, platform=port)
    assert isinstance(port.handle, _ReentrantLock)
    port.handle.store = store
    with pytest.raises(EpochStoreError, match="cleanup outcome is unknown"):
        store.close()
    with pytest.raises(EpochStoreError, match="cleanup outcome is unknown"):
        store.close()
    assert port.handle.closes == 1
    assert port.handle.nested_errors == ["epoch store cleanup outcome is unknown"]


def test_provision_failure_with_lock_cleanup_failure_reports_unknown_once(tmp_path, monkeypatch):
    port = _CountingPlatform(lock_type=_FailBeforeReleaseLock)
    root = tmp_path / "epoch"

    def fail_marker(*_args, **_kwargs):
        raise OSError("injected marker failure")

    monkeypatch.setattr(Path, "write_bytes", fail_marker)
    with pytest.raises(EpochStoreError, match="store provision cleanup outcome is unknown"):
        EpochStore.provision(root, PROVISION, BODY, platform=port)
    assert port.handle is not None and port.handle.closes == 1
    with pytest.raises(EpochStoreError):
        EpochStore.open(root, platform=port)
    with pytest.raises(EpochStoreError):
        EpochStore.provision(root, PROVISION, BODY, platform=port)
    assert port.handle.closes == 1


def test_open_validation_failure_with_lock_cleanup_failure_reports_unknown_once(tmp_path, monkeypatch):
    root = tmp_path / "epoch"
    provision_port = _CountingPlatform()
    store = EpochStore.provision(root, PROVISION, BODY, platform=provision_port)
    store.close()
    open_port = _CountingPlatform(lock_type=_FailBeforeReleaseLock)
    monkeypatch.setattr(EpochStore, "_read_row", lambda _self: (_ for _ in ()).throw(EpochStoreError("invalid row")))
    with pytest.raises(EpochStoreError, match="store open cleanup outcome is unknown"):
        EpochStore.open(root, platform=open_port)
    assert open_port.handle is not None and open_port.handle.closes == 1


def test_provision_failure_releases_lock_and_partial_install_cannot_open_or_reprovision(tmp_path, monkeypatch):
    port = _CountingPlatform()
    root = tmp_path / "epoch"

    def fail_marker(*_args, **_kwargs):
        raise OSError("injected marker failure")

    monkeypatch.setattr(Path, "write_bytes", fail_marker)
    with pytest.raises(EpochStoreError):
        EpochStore.provision(root, PROVISION, BODY, platform=port)


def test_configure_failure_releases_lock_and_keeps_partial_install_fail_closed(tmp_path, monkeypatch):
    port = _CountingPlatform()
    root = tmp_path / "epoch"

    def fail_configure(_connection):
        raise EpochStoreError("injected configure failure")

    monkeypatch.setattr("src.adapter.minecraft.epoch_store._configure", fail_configure)
    with pytest.raises(EpochStoreError):
        EpochStore.provision(root, PROVISION, BODY, platform=port)
    assert port.handle is not None and port.handle.closes == 1
    with pytest.raises(EpochStoreError):
        EpochStore.open(root, platform=port)
    with pytest.raises(EpochStoreError):
        EpochStore.provision(root, PROVISION, BODY, platform=port)
    assert port.handle is not None and port.handle.closes == 1
    with pytest.raises(EpochStoreError):
        EpochStore.open(root, platform=port)
    with pytest.raises(EpochStoreError):
        EpochStore.provision(root, PROVISION, BODY, platform=port)


def test_marker_schema_and_candidate_tampering_fail_closed(tmp_path):
    port = _TestPlatform()
    root = tmp_path / "epoch"
    store = EpochStore.provision(root, PROVISION, BODY, platform=port)
    store.close()
    (root / "provision.json").write_text(
        '{"version":1,"provisionId":"' + PROVISION + '","bodyId":"' + BODY + '","bodyId":"' + BODY + '"}'
    )
    with pytest.raises(EpochStoreError):
        EpochStore.open(root, platform=port)

    (root / "provision.json").write_text(json.dumps({"version": 1, "provisionId": PROVISION, "bodyId": BODY}))
    connection = sqlite3.connect(root / "epochs.sqlite3")
    connection.execute("CREATE TABLE unexpected (value INTEGER)")
    connection.commit()
    connection.close()
    with pytest.raises(EpochStoreError):
        EpochStore.open(root, platform=port)


@pytest.mark.parametrize(
    "last_epoch,generation,next_epoch", [(False, 0, 1), (0, False, 1), (0, 0, True), ((1 << 63) - 1, 0, 0), (0, 0, 2)]
)
def test_invalid_candidate_numbers_reject_before_database_mutation(tmp_path, last_epoch, generation, next_epoch):
    port = _TestPlatform()
    store = EpochStore.provision(tmp_path / "epoch", PROVISION, BODY, platform=port)
    with pytest.raises(ValueError):
        EpochCandidate(PROVISION, BODY, last_epoch, generation, next_epoch)
    assert store.read_candidate().next_epoch == 1
    store.close()


def test_commit_double_fault_closes_connection_and_preserves_unknown_disabled_state(tmp_path, monkeypatch):
    port = _TestPlatform()
    store = EpochStore.provision(tmp_path / "epoch", PROVISION, BODY, platform=port)
    candidate = store.read_candidate()
    original_connect = _connect_rw
    fault: _DoubleFaultConnection | None = None

    def faulting_connect(path):
        nonlocal fault
        fault = _DoubleFaultConnection(original_connect(path))
        return fault

    monkeypatch.setattr("src.adapter.minecraft.epoch_store._connect_rw", faulting_connect)
    with pytest.raises(EpochStoreError):
        store.commit_ready(candidate, ready(candidate))
    assert fault is not None and fault.closed == 1
    with pytest.raises(EpochStoreError):
        store.read_candidate()
    with pytest.raises(EpochStoreError):
        store.commit_ready(candidate, ready(candidate))
