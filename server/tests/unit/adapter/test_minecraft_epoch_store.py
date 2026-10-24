import json
import sqlite3
import threading
from pathlib import Path
from unittest.mock import Mock

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


def _custody_policy(path):
    from src.adapter.minecraft.epoch_store import TrustedEpochPolicy

    sid = b"\x01\x01\x00\x00\x00\x00\x00\x05\x12\x00\x00\x00"
    return TrustedEpochPolicy(str(path), 1, 2, sid, (sid,), (sid,), (sid,), PROVISION, BODY)


class _Lease:
    def __init__(self, marker):
        self.marker = marker
        self.closes = 0
        self.checks = 0
        self.failure = None
        self.on_close = lambda: None

    def read_marker(self):
        self.checks += 1
        return self.marker

    def validate(self):
        self.checks += 1

    def close(self):
        self.closes += 1
        self.on_close()
        if self.failure is not None:
            raise self.failure


def _custody_fixture(tmp_path):
    from src.adapter.minecraft.epoch_store import EpochOpenAttemptReceipt

    root = tmp_path / "custody"
    legacy = EpochStore.provision(root, PROVISION, BODY, platform=_TestPlatform())
    legacy.close()
    lease = _Lease((root / "provision.json").read_bytes())

    class Port:
        def acquire(self, policy, receipt):
            receipt._register(lease)
            return lease

    receipt = EpochOpenAttemptReceipt()
    store = EpochStore.open_with_custody(_custody_policy(root), custody=Port(), receipt=receipt)
    return store, lease, receipt


def test_custody_actual_store_transfer_read_cas_close(tmp_path):
    store, lease, receipt = _custody_fixture(tmp_path)
    assert receipt.snapshot().state == "TRANSFERRED"
    assert receipt.snapshot().retained_resources == 0
    candidate = store.read_candidate()
    result = store.commit_ready(candidate, VerifiedReadyContext(PROVISION, BODY, 0, 0, 1, "test-only"))
    assert result.last_epoch == 1
    assert lease.checks > 10
    store.close()
    store.close()
    assert lease.closes == 1


@pytest.mark.parametrize(
    "raw",
    [
        "relative",
        "C:relative",
        r"\\host\share",
        r"\\?\C:\root",
        r"C:\root\..\x",
        r"C:\root.\x",
        r"C:\CON\x",
        r"C:\root:x",
    ],
)
def test_custody_policy_rejects_original_lexical_alias(raw):
    with pytest.raises(EpochStoreError):
        _custody_policy(raw)


def test_custody_receipt_single_use_invalid_input_before_acquire():
    from src.adapter.minecraft.epoch_store import EpochOpenAttemptReceipt

    receipt = EpochOpenAttemptReceipt()
    custody = Mock()
    with pytest.raises(EpochStoreError):
        EpochStore.open_with_custody(None, custody=custody, receipt=receipt)
    assert receipt.snapshot().state == "RELEASED"
    with pytest.raises(EpochStoreError):
        EpochStore.open_with_custody(None, custody=custody, receipt=receipt)
    custody.acquire.assert_not_called()


@pytest.mark.parametrize("fatal", [False, True])
def test_custody_partial_acquire_retains_unknown_and_preserves_fatal(fatal):
    from src.adapter.minecraft.epoch_store import EpochOpenAttemptReceipt, EpochOpenCleanupError

    lease = _Lease(b"")
    original = KeyboardInterrupt() if fatal else RuntimeError()
    lease.failure = RuntimeError()

    class Port:
        def acquire(self, policy, receipt):
            receipt._register(lease)
            raise original

    receipt = EpochOpenAttemptReceipt()
    with pytest.raises(BaseException) as caught:
        EpochStore.open_with_custody(_custody_policy(r"C:\test"), custody=Port(), receipt=receipt)
    assert caught.value is original if fatal else isinstance(caught.value, EpochOpenCleanupError)
    assert receipt.snapshot().state == "UNKNOWN"
    assert receipt.snapshot().retained_resources == 1
    assert lease.closes == 1


@pytest.mark.parametrize("fatal", [False, True])
def test_custody_connection_close_unknown_no_readback_unlock_or_retry(tmp_path, monkeypatch, fatal):
    import src.adapter.minecraft.epoch_store as module

    store, lease, receipt = _custody_fixture(tmp_path)
    connection = Mock()
    original = KeyboardInterrupt() if fatal else RuntimeError()
    connection.close.side_effect = original
    monkeypatch.setattr(module, "_connect_rw", lambda path: connection)
    monkeypatch.setattr(module, "_configure", lambda value: None)
    monkeypatch.setattr(module, "_read_row", lambda *args: module._Row(1, PROVISION, BODY, 0, 0, b""))
    with pytest.raises(BaseException) as caught:
        store.read_candidate()
    if fatal:
        assert caught.value is original
    assert store.cleanup_evidence().retained_resources == 2
    assert receipt.snapshot().state == "TRANSFERRED"
    with pytest.raises(EpochStoreError):
        store.close()
    with pytest.raises(EpochStoreError):
        store.read_candidate()
    connection.close.assert_called_once()
    assert lease.closes == 0


def test_custody_close_reentry_terminal_unknown_once(tmp_path):
    store, lease, _ = _custody_fixture(tmp_path)

    def reenter():
        with pytest.raises(EpochStoreError):
            store.close()

    lease.on_close = reenter
    with pytest.raises(EpochStoreError):
        store.close()
    with pytest.raises(EpochStoreError):
        store.close()
    assert lease.closes == 1


def test_custody_legacy_provision_rejects_new_inputs_before_directory(monkeypatch):
    import src.adapter.minecraft.epoch_store as module

    directory = Mock(side_effect=AssertionError("must not resolve"))
    monkeypatch.setattr(module, "_directory", directory)
    with pytest.raises(EpochStoreError):
        EpochStore.provision(_custody_policy(r"C:\test"), PROVISION, BODY)

    class Port:
        def acquire(self, policy, receipt):
            raise AssertionError()

    with pytest.raises(EpochStoreError):
        EpochStore.provision(r"C:\test", PROVISION, BODY, platform=Port())
    directory.assert_not_called()


@pytest.mark.parametrize("boundary", ["BEGIN IMMEDIATE", "COMMIT", "POST_COMMIT", "READBACK"])
def test_custody_commit_boundaries_disable_without_retry(tmp_path, monkeypatch, boundary):
    import src.adapter.minecraft.epoch_store as module

    store, lease, _ = _custody_fixture(tmp_path)
    candidate = store.read_candidate()
    actual = module._connect_rw
    events = []
    connections = []
    original_validate = lease.validate

    def validate():
        if boundary == "POST_COMMIT" and "COMMIT" in events:
            raise RuntimeError("postcommit")
        original_validate()

    lease.validate = validate

    class Connection:
        def __init__(self, raw):
            self.raw = raw
            self.closes = 0

        def execute(self, sql, *args):
            events.append(sql)
            if sql == boundary:
                raise RuntimeError("boundary")
            return self.raw.execute(sql, *args)

        def close(self):
            self.closes += 1
            self.raw.close()

    def connect(path):
        if boundary == "READBACK" and connections:
            raise RuntimeError("readback")
        connection = Connection(actual(path))
        connections.append(connection)
        return connection

    monkeypatch.setattr(module, "_connect_rw", connect)
    with pytest.raises(EpochStoreError):
        store.commit_ready(candidate, VerifiedReadyContext(PROVISION, BODY, 0, 0, 1, "mock"))
    assert all(connection.closes == 1 for connection in connections)
    assert "ROLLBACK" not in events  # BEGIN failure is before a begun transaction; other failures after attempt
    assert store.cleanup_evidence().transaction_unknown == (boundary != "BEGIN IMMEDIATE")
    before = len(connections)
    with pytest.raises(EpochStoreError):
        store.commit_ready(candidate, VerifiedReadyContext(PROVISION, BODY, 0, 0, 1, "mock"))
    assert len(connections) == before
    store.close()
    assert lease.closes == 1


def test_custody_cleanup_fatal_outranks_ordinary_and_first_fatal_wins():
    from src.adapter.minecraft.epoch_store import EpochOpenAttemptReceipt

    first = KeyboardInterrupt()
    second = SystemExit()
    ordinary = RuntimeError()
    leases = [_Lease(b""), _Lease(b"")]
    leases[0].failure = second
    leases[1].failure = first

    class Port:
        def acquire(self, policy, receipt):
            for lease in leases:
                receipt._register(lease)
            raise ordinary

    receipt = EpochOpenAttemptReceipt()
    with pytest.raises(KeyboardInterrupt) as caught:
        EpochStore.open_with_custody(_custody_policy(r"C:\test"), custody=Port(), receipt=receipt)
    assert caught.value is first
    assert [lease.closes for lease in leases] == [1, 1]
    assert receipt.snapshot().retained_resources == 2


def test_custody_failed_open_connection_keeps_dependent_lease(monkeypatch):
    import src.adapter.minecraft.epoch_store as module

    lease = _Lease(json.dumps(dict(version=1, provisionId=PROVISION, bodyId=BODY)).encode())
    connection = Mock()
    connection.close.side_effect = RuntimeError()

    class Port:
        def acquire(self, policy, receipt):
            receipt._register(lease)
            return lease

    receipt = module.EpochOpenAttemptReceipt()
    monkeypatch.setattr(module, "_connect_rw", lambda path: connection)
    monkeypatch.setattr(module, "_configure", lambda connection: (_ for _ in ()).throw(ValueError()))
    with pytest.raises(module.EpochOpenCleanupError):
        EpochStore.open_with_custody(_custody_policy(r"C:\test"), custody=Port(), receipt=receipt)
    assert receipt.snapshot().state == "UNKNOWN"
    assert receipt.snapshot().retained_resources == 2
    connection.close.assert_called_once()
    assert lease.closes == 0


def test_custody_marker_is_after_lease_and_rechecked_before_connect(tmp_path, monkeypatch):
    import src.adapter.minecraft.epoch_store as module

    store, lease, _ = _custody_fixture(tmp_path)
    lease.marker = b"{}"
    connect = Mock(side_effect=AssertionError("no connect after marker mismatch"))
    monkeypatch.setattr(module, "_connect_rw", connect)
    with pytest.raises(EpochStoreError):
        store.read_candidate()
    connect.assert_not_called()
    store.close()
    assert lease.closes == 1


def test_custody_reentrant_connection_close_quarantines_without_duplicate(tmp_path, monkeypatch):
    import src.adapter.minecraft.epoch_store as module

    store, lease, _ = _custody_fixture(tmp_path)
    raw = module._connect_rw(store._directory / "epochs.sqlite3")
    closes = []

    class Connection:
        def execute(self, *args):
            return raw.execute(*args)

        def close(self):
            closes.append(1)
            with pytest.raises(EpochStoreError):
                store.close()
            raw.close()

    monkeypatch.setattr(module, "_connect_rw", lambda path: Connection())
    with pytest.raises(EpochStoreError):
        store.read_candidate()
    with pytest.raises(EpochStoreError):
        store.close()
    assert closes == [1]
    assert lease.closes == 0


@pytest.mark.parametrize("fatal", [False, True])
def test_custody_receipt_cleanup_closes_registration_window_before_callback(fatal):
    from src.adapter.minecraft.epoch_store import EpochOpenAttemptReceipt

    receipt = EpochOpenAttemptReceipt()
    lease = _Lease(b"")
    attempted_acquisition = []
    original = KeyboardInterrupt() if fatal else RuntimeError()

    def callback():
        # Port checks permission before obtaining any new resource: no orphan-on-reject.
        with pytest.raises(EpochStoreError):
            receipt._registration_window()
        with pytest.raises(EpochStoreError):
            receipt._register(lease)
        with pytest.raises(EpochStoreError):
            receipt._claim(object())
        with pytest.raises(EpochStoreError):
            receipt._finish("TRANSFERRED", object())
        assert attempted_acquisition == []

    lease.on_close = callback

    class Port:
        def acquire(self, policy, context):
            context._registration_window()
            context._register(lease)
            raise original

    with pytest.raises(BaseException) as caught:
        EpochStore.open_with_custody(_custody_policy(r"C:\test"), custody=Port(), receipt=receipt)
    if fatal:
        assert caught.value is original
    assert receipt.snapshot().state == "RELEASED"
    assert receipt.snapshot().retained_resources == 0
    assert lease.closes == 1
    with pytest.raises(EpochStoreError):
        receipt._finish("IN_PROGRESS", object())


@pytest.mark.parametrize("callback", ["validate", "read_marker"])
@pytest.mark.parametrize("nested", ["read", "commit"])
@pytest.mark.parametrize("outer", ["read", "commit"])
def test_custody_unified_operation_guard_rejects_caught_callback_reentry(
    tmp_path, monkeypatch, callback, nested, outer
):
    import src.adapter.minecraft.epoch_store as module

    store, lease, _ = _custody_fixture(tmp_path)
    candidate = store.read_candidate()
    ready = VerifiedReadyContext(PROVISION, BODY, 0, 0, 1, "mock")
    connect = Mock(side_effect=AssertionError("nested/disabled operation must not connect"))
    monkeypatch.setattr(module, "_connect_rw", connect)
    original = getattr(lease, callback)

    def reenter():
        with pytest.raises(EpochStoreError):
            store.read_candidate() if nested == "read" else store.commit_ready(candidate, ready)
        return original()

    setattr(lease, callback, reenter)
    with pytest.raises(EpochStoreError):
        store.read_candidate() if outer == "read" else store.commit_ready(candidate, ready)
    connect.assert_not_called()
    assert store.cleanup_evidence().state == "UNKNOWN"
    assert not store.cleanup_evidence().transaction_unknown
    with pytest.raises(EpochStoreError):
        store.close()
    assert lease.closes == 0


def test_custody_reentry_with_owned_connection_never_replaces_closes_or_rolls_back(tmp_path, monkeypatch):
    import src.adapter.minecraft.epoch_store as module

    store, lease, _ = _custody_fixture(tmp_path)
    candidate = store.read_candidate()
    ready = VerifiedReadyContext(PROVISION, BODY, 0, 0, 1, "mock")
    connection = Mock()
    connection.execute.return_value = Mock()
    connect = Mock(return_value=connection)
    monkeypatch.setattr(module, "_connect_rw", connect)
    monkeypatch.setattr(module, "_configure", lambda conn: None)
    original = lease.validate
    checks = []

    def validate():
        checks.append(1)
        if len(checks) == 4:  # after BEGIN, outer already owns its one connection
            with pytest.raises(EpochStoreError):
                store.read_candidate()
        original()

    lease.validate = validate
    with pytest.raises(EpochStoreError):
        store.commit_ready(candidate, ready)
    connect.assert_called_once()
    connection.close.assert_not_called()
    assert [args[0][0] for args in connection.execute.call_args_list] == ["BEGIN IMMEDIATE"]
    assert store.cleanup_evidence().retained_resources == 2
    with pytest.raises(EpochStoreError):
        store.close()
    assert lease.closes == 0


def test_custody_precommit_update_failure_rolls_back_once_with_outer_guard(tmp_path, monkeypatch):
    import src.adapter.minecraft.epoch_store as module

    store, lease, _ = _custody_fixture(tmp_path)
    candidate = store.read_candidate()
    actual = module._connect_rw
    events = []

    class Connection:
        def __init__(self, raw):
            self.raw = raw

        def execute(self, sql, *args):
            events.append(sql)
            if sql.startswith("UPDATE"):
                raise RuntimeError("update failed before commit")
            return self.raw.execute(sql, *args)

        def close(self):
            events.append("CLOSE")
            self.raw.close()

    monkeypatch.setattr(module, "_connect_rw", lambda path: Connection(actual(path)))
    with pytest.raises(EpochStoreError):
        store.commit_ready(candidate, VerifiedReadyContext(PROVISION, BODY, 0, 0, 1, "mock"))
    assert events.count("ROLLBACK") == 1
    assert events.count("CLOSE") == 1
    assert "COMMIT" not in events
    assert not store.cleanup_evidence().transaction_unknown
    store.close()
    assert lease.closes == 1


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
