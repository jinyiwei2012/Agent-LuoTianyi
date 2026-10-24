import asyncio
import copy
import gc
import json
import threading
import weakref
from pathlib import Path

import pytest

from src.adapter.minecraft.epoch_store import EpochCandidate, EpochStoreError, PlatformCapability
from src.adapter.minecraft.epoch_worker import (
    EpochCommitRequest,
    EpochJobHandle,
    EpochPreflightRequest,
    EpochPreflightSnapshot,
    EpochWorkerConfig,
    EpochWorkerError,
    EpochWorkerState,
    MinecraftEpochStoreWorker,
)

PROVISION = "10000000-0000-4000-8000-000000000001"
BODY = "20000000-0000-4000-8000-000000000002"
ROUTE = "30000000-0000-4000-8000-000000000003"
SERVER = "40000000-0000-4000-8000-000000000004"
CLIENT = "50000000-0000-4000-8000-000000000005"


def custody_config(path, custody):
    from src.adapter.minecraft.epoch_store import TrustedEpochPolicy

    sid = b"\x01\x01\x00\x00\x00\x00\x00\x05\x12\x00\x00\x00"
    policy = TrustedEpochPolicy(str(path), 1, 2, sid, (sid,), (sid,), (sid,), PROVISION, BODY)
    return EpochWorkerConfig(mode="OPEN_WITH_CUSTODY", directory=str(path), custody_policy=policy, custody=custody)


class CustodyLease:
    def __init__(self):
        self.calls = []
        self.failure = None

    def validate(self):
        self.calls.append(("validate", threading.get_ident()))

    def read_marker(self):
        self.calls.append(("marker", threading.get_ident()))
        return json.dumps(dict(version=1, provisionId=PROVISION, bodyId=BODY)).encode()

    def close(self):
        self.calls.append(("close", threading.get_ident()))
        if self.failure is not None:
            raise self.failure


@pytest.mark.asyncio
async def test_snapshot_partial_acquisition_never_claims_no_retention(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class Port:
        def acquire(self, policy, receipt):
            receipt._register(CustodyLease())
            entered.set()
            if not release.wait(2):
                raise RuntimeError("bounded test release")
            raise RuntimeError("deliberate acquisition failure")

    worker = MinecraftEpochStoreWorker(custody_config(tmp_path, Port()))

    async def start_caught():
        with pytest.raises(EpochWorkerError):
            await worker.start()

    task = asyncio.create_task(start_caught())
    try:
        # The loop must service start() before the synchronous event wait.
        ready = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(ready.set_result, None)
        await ready
        assert entered.wait(2)
        snapshot = worker.snapshot()
        assert not snapshot.terminal_published
        assert snapshot.retention == "NOT_YET_REPORTED"
    finally:
        release.set()
        await asyncio.wait_for(task, 3)
        await worker.close()
    assert worker.snapshot().retention == "NONE"


@pytest.mark.asyncio
async def test_snapshot_available_store_never_claims_no_retention(tmp_path):
    from src.adapter.minecraft.epoch_store import EpochStore

    root = tmp_path / "epoch"
    legacy = EpochStore.provision(root, PROVISION, BODY, platform=Platform())
    legacy.close()

    class Port:
        def acquire(self, policy, receipt):
            lease = CustodyLease()
            receipt._register(lease)
            return lease

    worker = MinecraftEpochStoreWorker(custody_config(root, Port()))
    try:
        await worker.start()
        assert worker.state is EpochWorkerState.AVAILABLE
        assert not worker.snapshot().terminal_published
        assert worker.snapshot().retention == "NOT_YET_REPORTED"
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_start_cancel_and_acquisition_fatal_drains_then_direct_rethrows(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    fatal = KeyboardInterrupt("acquisition")
    lease = CustodyLease()
    lease.failure = RuntimeError("cleanup")

    class Port:
        def acquire(self, policy, receipt):
            receipt._register(lease)
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test timeout")
            raise fatal

    worker = MinecraftEpochStoreWorker(custody_config(tmp_path, Port()))
    observed = []

    async def direct_caller():
        try:
            await worker.start()
        except KeyboardInterrupt as caught:
            assert caught is fatal
            observed.append(worker.snapshot())
        else:
            pytest.fail("fatal must win cancellation")

    task = asyncio.create_task(direct_caller())
    try:
        ready = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(ready.set_result, None)
        await ready
        assert entered.wait(2)
        task.cancel()
    finally:
        release.set()
        await asyncio.wait_for(task, 3)
    assert observed[0].terminal_published and observed[0].thread_completion_observed
    assert observed[0].cleanup == "UNKNOWN" and observed[0].retention == "OPEN_RECEIPT"
    assert isinstance(worker._start_task.exception(), EpochWorkerError)
    assert isinstance(worker._close_task.exception(), EpochWorkerError)
    try:
        await worker.close()
    except KeyboardInterrupt as caught:
        assert caught is fatal
    else:
        pytest.fail("retained fatal missing")


@pytest.mark.asyncio
async def test_first_operation_fatal_wins_cleanup_fatal_at_direct_close(tmp_path, monkeypatch):
    from src.adapter.minecraft.epoch_store import EpochStore

    root = tmp_path / "epoch"
    legacy = EpochStore.provision(root, PROVISION, BODY, platform=Platform())
    legacy.close()
    first = KeyboardInterrupt("operation")
    later = SystemExit("cleanup")

    class Port:
        def acquire(self, policy, receipt):
            lease = CustodyLease()
            lease.failure = later
            receipt._register(lease)
            return lease

    worker = MinecraftEpochStoreWorker(custody_config(root, Port()))
    await worker.start()

    def fail_read(store):
        raise first

    monkeypatch.setattr(EpochStore, "read_candidate", fail_read)
    handle = worker.submit_preflight(preflight())
    with pytest.raises(EpochWorkerError):
        await worker.wait(handle)
    try:
        await worker.close()
    except KeyboardInterrupt as caught:
        assert caught is first
    else:
        pytest.fail("first fatal missing")
    assert worker.snapshot().terminal_published and worker.snapshot().thread_completion_observed
    assert worker.snapshot().retention == "STORE"


@pytest.mark.asyncio
async def test_evidence_fatal_retains_store_and_publishes_unknown(tmp_path, monkeypatch):
    from src.adapter.minecraft.epoch_store import EpochStore

    root = tmp_path / "epoch"
    legacy = EpochStore.provision(root, PROVISION, BODY, platform=Platform())
    legacy.close()

    class Port:
        def acquire(self, policy, receipt):
            lease = CustodyLease()
            receipt._register(lease)
            return lease

    worker = MinecraftEpochStoreWorker(custody_config(root, Port()))
    await worker.start()
    reference = weakref.ref(worker._retained_store)
    fatal = SystemExit("evidence")

    def fail_evidence(store):
        raise fatal

    monkeypatch.setattr(EpochStore, "cleanup_evidence", fail_evidence)
    try:
        await worker.close()
    except SystemExit as caught:
        assert caught is fatal
    else:
        pytest.fail("evidence fatal missing")
    assert worker.snapshot().terminal_published and worker.snapshot().thread_completion_observed
    assert worker.snapshot().cleanup == "UNKNOWN" and worker.snapshot().retention == "STORE"
    # Remove traceback references so ownership evidence is not accidentally supplied by fatal frames.
    fatal.__traceback__ = None
    gc.collect()
    assert reference() is worker._retained_store


def test_custody_config_exact_and_inert(tmp_path):
    class Port:
        def acquire(self, *args):
            raise AssertionError("constructor must be inert")

    config = custody_config(tmp_path, Port())
    assert config.directory == str(tmp_path)
    from dataclasses import replace

    for changes in (
        dict(directory=tmp_path),
        dict(platform=Platform()),
        dict(provision_id=PROVISION),
        dict(mode="OPEN"),
        dict(custody=None),
        dict(directory=str(tmp_path) + "\\other"),
    ):
        with pytest.raises(ValueError):
            replace(config, **changes)


@pytest.mark.asyncio
async def test_actual_custody_worker_success_same_thread_and_public_evidence(tmp_path):
    from src.adapter.minecraft.epoch_store import EpochStore

    legacy = EpochStore.provision(tmp_path / "epoch", PROVISION, BODY, platform=Platform())
    legacy.close()
    lease = CustodyLease()
    acquired = []

    class Port:
        def acquire(self, policy, receipt):
            acquired.append(threading.get_ident())
            receipt._registration_window()
            receipt._register(lease)
            return lease

    worker = MinecraftEpochStoreWorker(custody_config(tmp_path / "epoch", Port()))
    await worker.start()
    handle = worker.submit_preflight(preflight())
    await worker.wait(handle)
    worker.mark_obsolete(worker.take_preflight(handle))
    await worker.close()
    assert worker.state is EpochWorkerState.CLOSED
    assert worker.snapshot().cleanup == "CONFIRMED"
    assert worker.snapshot().retention == "NONE"
    assert set(acquired + [ident for _, ident in lease.calls]) == {worker._thread.ident}
    assert worker._thread.ident != threading.get_ident() and not worker._thread.daemon


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
async def test_custody_startup_receipt_retention_after_thread_death(tmp_path, unknown):
    refs = []

    class Port:
        def acquire(self, policy, receipt):
            lease = CustodyLease()
            refs.append(weakref.ref(lease))
            receipt._register(lease)
            if unknown:
                lease.failure = RuntimeError("unknown")
            raise RuntimeError("acquire failed")

    worker = MinecraftEpochStoreWorker(custody_config(tmp_path, Port()))
    with pytest.raises(EpochWorkerError):
        await worker.start()
    gc.collect()
    assert worker.snapshot().thread_completion_observed
    assert worker.snapshot().cleanup == ("UNKNOWN" if unknown else "CONFIRMED")
    assert worker.snapshot().retention == ("OPEN_RECEIPT" if unknown else "NONE")
    assert (refs[0]() is not None) == unknown
    with pytest.raises(EpochWorkerError):
        await worker.start()
    if unknown:
        with pytest.raises(EpochWorkerError):
            await worker.close()
        assert len(refs[0]().calls) == 1
    else:
        await worker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("reject", ["startup", "job", "terminal"])
async def test_custody_notification_rejection_independent_terminal(tmp_path, monkeypatch, reject):
    from src.adapter.minecraft.epoch_store import EpochStore

    root = tmp_path / "epoch"
    legacy = EpochStore.provision(root, PROVISION, BODY, platform=Platform())
    legacy.close()
    lease = CustodyLease()

    class Port:
        def acquire(self, policy, receipt):
            receipt._register(lease)
            return lease

    worker = MinecraftEpochStoreWorker(custody_config(root, Port()))
    original = worker._post

    def post(callback, *args):
        names = {"startup": "_owner_started", "job": "_owner_complete", "terminal": "_owner_terminal"}
        return False if callback.__name__ == names[reject] else original(callback, *args)

    monkeypatch.setattr(worker, "_post", post)
    if reject == "startup":
        with pytest.raises(EpochWorkerError):
            await worker.start()
    else:
        await worker.start()
        if reject == "job":
            handle = worker.submit_preflight(preflight())
            with pytest.raises(EpochWorkerError):
                await worker.wait(handle)
    await worker.close()
    snapshot = worker.snapshot()
    assert snapshot.terminal_published and snapshot.thread_completion_observed
    assert snapshot.cleanup == "CONFIRMED"
    assert snapshot.notification == ("REJECTED" if reject == "terminal" else "ACCEPTED")


@pytest.mark.asyncio
async def test_custody_fatal_released_direct_await_same_object_no_internal_fatal(tmp_path):
    fatal = KeyboardInterrupt("test fatal")

    class Port:
        def acquire(self, policy, receipt):
            raise fatal

    worker = MinecraftEpochStoreWorker(custody_config(tmp_path, Port()))
    # Catch INSIDE direct-await coroutine; never put KeyboardInterrupt into an asyncio Task.
    try:
        await worker.start()
    except KeyboardInterrupt as caught:
        assert caught is fatal
    else:
        pytest.fail("fatal missing")
    assert worker.snapshot().cleanup == "CONFIRMED"
    assert not worker._thread.is_alive()
    assert isinstance(worker._start_task.exception(), EpochWorkerError)
    try:
        await worker.close()
    except KeyboardInterrupt as caught:
        assert caught is fatal
    else:
        pytest.fail("fatal missing on repeated lifecycle")


@pytest.mark.asyncio
async def test_custody_native_unsupported_worker_zero_native_calls(tmp_path, monkeypatch):
    from src.adapter.minecraft.epoch_platform import WindowsEpochPlatform

    def forbidden(*args, **kwargs):
        raise AssertionError("no native fallback")

    monkeypatch.setattr("src.adapter.minecraft.epoch_platform._kernel32", forbidden)
    monkeypatch.setattr("src.adapter.minecraft.epoch_store._connect_rw", forbidden)
    worker = MinecraftEpochStoreWorker(custody_config(tmp_path, WindowsEpochPlatform()))
    with pytest.raises(EpochWorkerError):
        await worker.start()
    assert worker.snapshot().cleanup == "CONFIRMED"
    await worker.close()


@pytest.mark.asyncio
async def test_custody_later_lease_unknown_retained_after_thread_death(tmp_path):
    from src.adapter.minecraft.epoch_store import EpochStore

    root = tmp_path / "epoch"
    legacy = EpochStore.provision(root, PROVISION, BODY, platform=Platform())
    legacy.close()
    refs = []

    class Port:
        def acquire(self, policy, receipt):
            lease = CustodyLease()
            lease.failure = RuntimeError("close unknown")
            refs.append(weakref.ref(lease))
            receipt._register(lease)
            return lease

    worker = MinecraftEpochStoreWorker(custody_config(root, Port()))
    await worker.start()
    with pytest.raises(EpochWorkerError):
        await worker.close()
    gc.collect()
    assert worker.snapshot().retention == "STORE" and worker.snapshot().cleanup == "UNKNOWN"
    assert refs[0]() is not None
    with pytest.raises(EpochWorkerError):
        await worker.close()
    assert sum(name == "close" for name, _ in refs[0]().calls) == 1


class Lock:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class Platform:
    def __init__(self):
        self.lock = Lock()

    def capability(self, directory):
        return PlatformCapability(True, "test-only verified")

    def acquire_lock(self, path):
        return self.lock


def preflight():
    return EpochPreflightRequest(
        provision_id=PROVISION,
        body_id=BODY,
        coordinator_generation=1,
        route_id=ROUTE,
        server_connection_id=SERVER,
        controller_connection_id=CLIENT,
    )


@pytest.mark.asyncio
async def test_provision_preflight_commit_one_use_and_bounded_handoff(tmp_path):
    worker = MinecraftEpochStoreWorker(
        EpochWorkerConfig(
            mode="PROVISION",
            directory=(tmp_path / "epoch").resolve(),
            platform=Platform(),
            provision_id=PROVISION,
            body_id=BODY,
        )
    )
    await worker.start()
    assert worker.state is EpochWorkerState.AVAILABLE
    handle = worker.submit_preflight(preflight())
    await worker.wait(handle)
    snapshot = worker.take_preflight(handle)
    assert worker.outstanding_count == 1 and snapshot.next_epoch == 1
    with pytest.raises(EpochWorkerError):
        worker.take_preflight(handle)
    commit = worker.submit_commit(
        EpochCommitRequest(
            preflight=snapshot,
            evidence_id="ready-evidence",
            pending_identity=object(),
            coordinator_generation=1,
            route_id=ROUTE,
            server_connection_id=SERVER,
            controller_connection_id=CLIENT,
        )
    )
    assert worker.outstanding_count == 1
    await worker.wait(commit)
    committed = worker.take_commit(commit)
    assert (committed.last_epoch, committed.generation) == (1, 1)
    assert worker.outstanding_count == 0
    await worker.close()
    assert worker.state is EpochWorkerState.CLOSED


@pytest.mark.asyncio
async def test_global_capacity_includes_delivered_preflight_and_wrong_handles_do_not_release(tmp_path):
    worker = MinecraftEpochStoreWorker(
        EpochWorkerConfig(
            mode="PROVISION",
            directory=(tmp_path / "epoch").resolve(),
            platform=Platform(),
            provision_id=PROVISION,
            body_id=BODY,
        )
    )
    await worker.start()
    handles = [worker.submit_preflight(preflight()) for _ in range(2)]
    for handle in handles:
        await worker.wait(handle)
        worker.take_preflight(handle)
    with pytest.raises(EpochWorkerError, match="capacity"):
        worker.submit_preflight(preflight())
    with pytest.raises(EpochWorkerError):
        worker.mark_obsolete(EpochJobHandle(object()))
    assert worker.outstanding_count == 2
    worker.mark_obsolete(handles[0])
    assert worker.outstanding_count == 1
    worker.mark_obsolete(handles[1])
    await worker.close()


@pytest.mark.asyncio
async def test_default_platform_remains_unavailable_and_no_fallback(tmp_path):
    worker = MinecraftEpochStoreWorker(EpochWorkerConfig(mode="OPEN", directory=(tmp_path / "missing").resolve()))
    with pytest.raises(EpochWorkerError, match="unavailable"):
        await worker.start()
    assert worker.state is EpochWorkerState.UNAVAILABLE
    await worker.close()
    assert worker.state is EpochWorkerState.CLOSED


def test_config_and_request_types_are_exact():
    with pytest.raises(ValueError):
        EpochWorkerConfig(mode="PROVISION", directory=Path("relative"), provision_id=PROVISION, body_id=BODY)
    for bad in (True, 1.0, "1", None):
        with pytest.raises(ValueError):
            EpochPreflightRequest(
                provision_id=PROVISION,
                body_id=BODY,
                coordinator_generation=bad,
                route_id=ROUTE,
                server_connection_id=SERVER,
                controller_connection_id=CLIENT,
            )


@pytest.mark.asyncio
async def test_validate_preflight_is_exact_nonconsuming_and_callback_free(tmp_path):
    worker = MinecraftEpochStoreWorker(
        EpochWorkerConfig(
            mode="PROVISION",
            directory=(tmp_path / "epoch").resolve(),
            platform=Platform(),
            provision_id=PROVISION,
            body_id=BODY,
        )
    )
    await worker.start()
    handle = worker.submit_preflight(preflight())
    assert not worker.validates_preflight(
        EpochPreflightSnapshot(PROVISION, BODY, 0, 0, 1, 1, ROUTE, SERVER, CLIENT, object(), object()),
        provision_id=PROVISION,
        body_id=BODY,
        coordinator_generation=1,
        route_id=ROUTE,
        server_connection_id=SERVER,
        controller_connection_id=CLIENT,
    )
    await worker.wait(handle)
    snapshot = worker.take_preflight(handle)
    expected = dict(
        provision_id=PROVISION,
        body_id=BODY,
        coordinator_generation=1,
        route_id=ROUTE,
        server_connection_id=SERVER,
        controller_connection_id=CLIENT,
    )
    assert worker.validates_preflight(snapshot, **expected)
    assert worker.validates_preflight(snapshot, **expected)
    assert worker.outstanding_count == 1
    for key, value in (
        ("provision_id", BODY),
        ("body_id", PROVISION),
        ("coordinator_generation", 2),
        ("route_id", SERVER),
        ("server_connection_id", CLIENT),
        ("controller_connection_id", SERVER),
    ):
        changed = dict(expected)
        changed[key] = value
        assert not worker.validates_preflight(snapshot, **changed)
    assert not worker.validates_preflight(copy.copy(snapshot), **expected)
    worker.mark_obsolete(snapshot)
    assert not worker.validates_preflight(snapshot, **expected)
    await worker.close()


@pytest.mark.asyncio
async def test_preflight_requested_identity_must_match_actual_store(tmp_path):
    worker = MinecraftEpochStoreWorker(
        EpochWorkerConfig(
            mode="PROVISION",
            directory=(tmp_path / "epoch").resolve(),
            platform=Platform(),
            provision_id=PROVISION,
            body_id=BODY,
        )
    )
    await worker.start()
    wrong = EpochPreflightRequest(
        provision_id="60000000-0000-4000-8000-000000000006",
        body_id=BODY,
        coordinator_generation=1,
        route_id=ROUTE,
        server_connection_id=SERVER,
        controller_connection_id=CLIENT,
    )
    handle = worker.submit_preflight(wrong)
    with pytest.raises(EpochWorkerError, match="job failed"):
        await worker.wait(handle)
    assert worker.state is EpochWorkerState.UNAVAILABLE
    await worker.close()


@pytest.mark.asyncio
async def test_malicious_snapshot_identity_hash_and_attributes_are_never_invoked(tmp_path):
    worker = MinecraftEpochStoreWorker(
        EpochWorkerConfig(
            mode="PROVISION",
            directory=(tmp_path / "epoch").resolve(),
            platform=Platform(),
            provision_id=PROVISION,
            body_id=BODY,
        )
    )
    await worker.start()
    handle = worker.submit_preflight(preflight())
    await worker.wait(handle)
    genuine = worker.take_preflight(handle)

    class Explosive:
        def __hash__(self):
            raise AssertionError("hash callback")

        def __eq__(self, other):
            raise AssertionError("equality callback")

    forged = EpochPreflightSnapshot(
        genuine.provision_id,
        genuine.body_id,
        genuine.expected_last_epoch,
        genuine.expected_generation,
        genuine.next_epoch,
        genuine.coordinator_generation,
        genuine.route_id,
        genuine.server_connection_id,
        genuine.controller_connection_id,
        [],
        Explosive(),
    )
    expected = dict(
        provision_id=PROVISION,
        body_id=BODY,
        coordinator_generation=1,
        route_id=ROUTE,
        server_connection_id=SERVER,
        controller_connection_id=CLIENT,
    )
    assert worker.validates_preflight(forged, **expected) is False

    class SnapshotSubclass(EpochPreflightSnapshot):
        def __getattribute__(self, name):
            raise AssertionError("attribute callback")

    subclass = object.__new__(SnapshotSubclass)
    assert worker.validates_preflight(subclass, **expected) is False
    worker.mark_obsolete(genuine)
    await worker.close()


@pytest.mark.asyncio
async def test_copied_handle_and_snapshot_cannot_consume_or_convert(tmp_path):
    worker = MinecraftEpochStoreWorker(
        EpochWorkerConfig(
            mode="PROVISION",
            directory=(tmp_path / "epoch").resolve(),
            platform=Platform(),
            provision_id=PROVISION,
            body_id=BODY,
        )
    )
    await worker.start()
    handle = worker.submit_preflight(preflight())
    await worker.wait(handle)
    with pytest.raises(EpochWorkerError):
        worker.take_preflight(copy.copy(handle))
    snapshot = worker.take_preflight(handle)
    with pytest.raises(EpochWorkerError):
        worker.submit_commit(
            EpochCommitRequest(
                preflight=copy.copy(snapshot),
                evidence_id="evidence",
                pending_identity=object(),
                coordinator_generation=1,
                route_id=ROUTE,
                server_connection_id=SERVER,
                controller_connection_id=CLIENT,
            )
        )
    worker.mark_obsolete(snapshot)
    await worker.close()


@pytest.mark.asyncio
async def test_wait_cancellation_does_not_cancel_shared_job(tmp_path):
    worker = MinecraftEpochStoreWorker(
        EpochWorkerConfig(
            mode="PROVISION",
            directory=(tmp_path / "epoch").resolve(),
            platform=Platform(),
            provision_id=PROVISION,
            body_id=BODY,
        )
    )
    await worker.start()
    handle = worker.submit_preflight(preflight())
    waiter = asyncio.create_task(worker.wait(handle))
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await worker.wait(handle)
    snapshot = worker.take_preflight(handle)
    worker.mark_obsolete(snapshot)
    await worker.close()


@pytest.mark.asyncio
async def test_wrong_thread_owner_gate_precedes_state_change(tmp_path):
    worker = MinecraftEpochStoreWorker(
        EpochWorkerConfig(
            mode="PROVISION",
            directory=(tmp_path / "epoch").resolve(),
            platform=Platform(),
            provision_id=PROVISION,
            body_id=BODY,
        )
    )
    await worker.start()
    errors = []

    def wrong_thread():
        try:
            asyncio.run(worker.close())
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=wrong_thread)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive() and isinstance(errors[0], EpochWorkerError)
    assert worker.state is EpochWorkerState.AVAILABLE
    await worker.close()
    assert worker._thread is not None and not worker._thread.is_alive()


def test_constructor_is_lexical_and_does_not_probe_filesystem(monkeypatch, tmp_path):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("constructor filesystem probe")

    monkeypatch.setattr(Path, "is_symlink", forbidden)
    config = EpochWorkerConfig(mode="OPEN", directory=(tmp_path / "future").resolve())
    worker = MinecraftEpochStoreWorker(config)
    assert worker._state is EpochWorkerState.NEW and worker._thread is None


@pytest.mark.asyncio
async def test_all_store_operations_run_on_one_worker_thread(monkeypatch, tmp_path):
    calls = []
    owner_thread = threading.get_ident()

    class Store:
        def read_candidate(self):
            calls.append(("read", threading.get_ident()))
            return EpochCandidate(PROVISION, BODY, 0, 0, 1)

        def commit_ready(self, candidate, ready):
            calls.append(("commit", threading.get_ident()))
            return type(
                "Committed", (), {"provision_id": PROVISION, "body_id": BODY, "last_epoch": 1, "generation": 1}
            )()

        def close(self):
            calls.append(("close", threading.get_ident()))

    monkeypatch.setattr(
        "src.adapter.minecraft.epoch_worker.EpochStore.provision",
        lambda *args, **kwargs: calls.append(("provision", threading.get_ident())) or Store(),
    )
    worker = MinecraftEpochStoreWorker(
        EpochWorkerConfig(
            mode="PROVISION",
            directory=(tmp_path / "epoch").resolve(),
            platform=Platform(),
            provision_id=PROVISION,
            body_id=BODY,
        )
    )
    await worker.start()
    handle = worker.submit_preflight(preflight())
    await worker.wait(handle)
    snapshot = worker.take_preflight(handle)
    pending = object()
    commit = worker.submit_commit(
        EpochCommitRequest(
            preflight=snapshot,
            evidence_id="evidence",
            pending_identity=pending,
            coordinator_generation=1,
            route_id=ROUTE,
            server_connection_id=SERVER,
            controller_connection_id=CLIENT,
        )
    )
    await worker.wait(commit)
    result = worker.take_commit(commit)
    assert result.pending_identity is pending
    await worker.close()
    thread_ids = {thread_id for _, thread_id in calls}
    assert len(thread_ids) == 1 and owner_thread not in thread_ids
    assert [name for name, _ in calls] == ["provision", "read", "commit", "close"]


@pytest.mark.asyncio
async def test_first_job_failure_stops_queued_work_and_settles_all_waiters(monkeypatch, tmp_path):
    started = threading.Event()
    release = threading.Event()
    commit_calls = []

    class Store:
        def read_candidate(self):
            started.set()
            release.wait(timeout=2)
            raise EpochStoreError("raw backend detail")

        def commit_ready(self, candidate, ready):
            commit_calls.append(True)
            raise AssertionError

        def close(self):
            return None

    monkeypatch.setattr("src.adapter.minecraft.epoch_worker.EpochStore.provision", lambda *a, **k: Store())
    worker = MinecraftEpochStoreWorker(
        EpochWorkerConfig(
            mode="PROVISION",
            directory=(tmp_path / "epoch").resolve(),
            platform=Platform(),
            provision_id=PROVISION,
            body_id=BODY,
        )
    )
    await worker.start()
    first = worker.submit_preflight(preflight())
    assert started.wait(timeout=2)
    second = worker.submit_preflight(preflight())
    release.set()
    for handle in (first, second):
        with pytest.raises(EpochWorkerError, match="job failed"):
            await asyncio.wait_for(worker.wait(handle), timeout=2)
    assert commit_calls == [] and worker.state is EpochWorkerState.UNAVAILABLE
    await worker.close()
    assert worker.state is EpochWorkerState.CLOSED


@pytest.mark.asyncio
async def test_startup_cleanup_unknown_is_terminal_and_never_retried(monkeypatch, tmp_path):
    calls = []

    def fail(*args, **kwargs):
        calls.append(True)
        raise EpochStoreError("store open cleanup outcome is unknown")

    monkeypatch.setattr("src.adapter.minecraft.epoch_worker.EpochStore.open", fail)
    worker = MinecraftEpochStoreWorker(
        EpochWorkerConfig(mode="OPEN", directory=(tmp_path / "epoch").resolve(), platform=Platform())
    )
    with pytest.raises(EpochWorkerError, match="unavailable"):
        await worker.start()
    assert worker.state is EpochWorkerState.CLEANUP_UNKNOWN and calls == [True]
    with pytest.raises(EpochWorkerError, match="cleanup outcome is unknown"):
        await worker.close()
    assert calls == [True] and worker._thread is not None and not worker._thread.is_alive()


@pytest.mark.asyncio
async def test_close_waits_for_worker_thread_and_cancellation_does_not_abandon(monkeypatch, tmp_path):
    close_entered = threading.Event()
    close_release = threading.Event()

    class Store:
        def close(self):
            close_entered.set()
            close_release.wait(timeout=2)

    monkeypatch.setattr("src.adapter.minecraft.epoch_worker.EpochStore.provision", lambda *a, **k: Store())
    worker = MinecraftEpochStoreWorker(
        EpochWorkerConfig(
            mode="PROVISION",
            directory=(tmp_path / "epoch").resolve(),
            platform=Platform(),
            provision_id=PROVISION,
            body_id=BODY,
        )
    )
    await worker.start()
    closing = asyncio.create_task(worker.close())
    while not close_entered.is_set():
        await asyncio.sleep(0)
    assert worker.state is EpochWorkerState.CLOSING and worker._thread.is_alive()
    closing.cancel()
    await asyncio.sleep(0)
    assert worker._thread.is_alive() and worker.state is EpochWorkerState.CLOSING
    close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closing, timeout=2)
    await worker.close()
    assert worker.state is EpochWorkerState.CLOSED and not worker._thread.is_alive()


@pytest.mark.asyncio
async def test_worker_fatal_settles_running_and_queued_waiters_and_cleans_metadata(monkeypatch, tmp_path):
    entered = threading.Event()
    release = threading.Event()
    close_calls = []
    second_calls = []

    class WorkerFatal(BaseException):
        pass

    class Store:
        def read_candidate(self):
            if not entered.is_set():
                entered.set()
                release.wait(timeout=2)
                raise WorkerFatal()
            second_calls.append(True)
            return EpochCandidate(PROVISION, BODY, 0, 0, 1)

        def close(self):
            close_calls.append(threading.get_ident())

    monkeypatch.setattr("src.adapter.minecraft.epoch_worker.EpochStore.provision", lambda *a, **k: Store())
    worker = MinecraftEpochStoreWorker(
        EpochWorkerConfig(
            mode="PROVISION",
            directory=(tmp_path / "epoch").resolve(),
            platform=Platform(),
            provision_id=PROVISION,
            body_id=BODY,
        )
    )
    await worker.start()
    first = worker.submit_preflight(preflight())
    assert entered.wait(timeout=2)
    second = worker.submit_preflight(preflight())
    release.set()
    for handle in (first, second):
        with pytest.raises(EpochWorkerError, match="job failed"):
            await asyncio.wait_for(worker.wait(handle), timeout=2)
    await asyncio.sleep(0)
    assert worker.state is EpochWorkerState.CLEANUP_UNKNOWN
    assert second_calls == [] and len(close_calls) == 1
    assert worker._claimed == set() and worker._obsolete == set()
    assert worker._thread is not None and not worker._thread.is_alive()
    # Public lifecycle delivery deliberately rethrows the retained fatal, not through internal tasks.
    with pytest.raises(WorkerFatal):
        await worker.close()


@pytest.mark.asyncio
async def test_obsolete_metadata_lifetime_is_bounded_and_empty_after_ack(tmp_path):
    worker = MinecraftEpochStoreWorker(
        EpochWorkerConfig(
            mode="PROVISION",
            directory=(tmp_path / "epoch").resolve(),
            platform=Platform(),
            provision_id=PROVISION,
            body_id=BODY,
        )
    )
    await worker.start()
    for _ in range(8):
        handle = worker.submit_preflight(preflight())
        worker.mark_obsolete(handle)
        await worker.wait(handle)
        await asyncio.sleep(0)
        assert len(worker._obsolete) <= 2 and len(worker._claimed) <= 2
        assert worker.outstanding_count == 0
    assert worker._obsolete == set() and worker._claimed == set()
    await worker.close()
    assert worker._obsolete == set() and worker._claimed == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ("ordinary", "unknown"))
async def test_cancelled_start_always_drains_startup_and_close(monkeypatch, tmp_path, failure):
    open_entered = threading.Event()
    open_release = threading.Event()
    close_calls = []

    class Store:
        def close(self):
            close_calls.append(True)

    def open_store(*args, **kwargs):
        open_entered.set()
        open_release.wait(timeout=2)
        if failure == "ordinary":
            raise EpochStoreError("open failed")
        if failure == "unknown":
            raise EpochStoreError("store open cleanup outcome is unknown")
        return Store()

    monkeypatch.setattr("src.adapter.minecraft.epoch_worker.EpochStore.open", open_store)
    worker = MinecraftEpochStoreWorker(
        EpochWorkerConfig(mode="OPEN", directory=(tmp_path / "epoch").resolve(), platform=Platform())
    )
    task = asyncio.create_task(worker.start())
    while not open_entered.is_set():
        await asyncio.sleep(0)
    task.cancel()
    asyncio.get_running_loop().call_soon(task.cancel)
    open_release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert worker._thread is not None and not worker._thread.is_alive()
    expected = EpochWorkerState.CLEANUP_UNKNOWN if failure == "unknown" else EpochWorkerState.CLOSED
    assert worker.state is expected
    assert close_calls == []


def _assert_fully_closed(worker):
    assert worker.state is EpochWorkerState.CLOSED
    assert worker.outstanding_count == 0
    assert worker._jobs == {}
    assert worker._claimed == set()
    assert worker._obsolete == set()
    assert worker._thread is not None and not worker._thread.is_alive()


@pytest.mark.asyncio
async def test_close_clears_completed_unconsumed_preflight(tmp_path):
    worker = MinecraftEpochStoreWorker(
        EpochWorkerConfig(
            mode="PROVISION",
            directory=(tmp_path / "epoch").resolve(),
            platform=Platform(),
            provision_id=PROVISION,
            body_id=BODY,
        )
    )
    await worker.start()
    handle = worker.submit_preflight(preflight())
    await worker.wait(handle)
    assert worker.outstanding_count == 1
    await worker.close()
    _assert_fully_closed(worker)
    with pytest.raises(EpochWorkerError):
        worker.take_preflight(handle)


@pytest.mark.asyncio
async def test_close_clears_delivered_preflight_and_snapshot_cannot_commit(tmp_path):
    worker = MinecraftEpochStoreWorker(
        EpochWorkerConfig(
            mode="PROVISION",
            directory=(tmp_path / "epoch").resolve(),
            platform=Platform(),
            provision_id=PROVISION,
            body_id=BODY,
        )
    )
    await worker.start()
    handle = worker.submit_preflight(preflight())
    await worker.wait(handle)
    snapshot = worker.take_preflight(handle)
    await worker.close()
    _assert_fully_closed(worker)
    with pytest.raises(EpochWorkerError, match="unavailable"):
        worker.submit_commit(
            EpochCommitRequest(
                preflight=snapshot,
                evidence_id="evidence",
                pending_identity=object(),
                coordinator_generation=1,
                route_id=ROUTE,
                server_connection_id=SERVER,
                controller_connection_id=CLIENT,
            )
        )


@pytest.mark.asyncio
async def test_close_clears_completed_unconsumed_commit(tmp_path):
    worker = MinecraftEpochStoreWorker(
        EpochWorkerConfig(
            mode="PROVISION",
            directory=(tmp_path / "epoch").resolve(),
            platform=Platform(),
            provision_id=PROVISION,
            body_id=BODY,
        )
    )
    await worker.start()
    preflight_handle = worker.submit_preflight(preflight())
    await worker.wait(preflight_handle)
    snapshot = worker.take_preflight(preflight_handle)
    commit_handle = worker.submit_commit(
        EpochCommitRequest(
            preflight=snapshot,
            evidence_id="evidence",
            pending_identity=object(),
            coordinator_generation=1,
            route_id=ROUTE,
            server_connection_id=SERVER,
            controller_connection_id=CLIENT,
        )
    )
    await worker.wait(commit_handle)
    assert worker.outstanding_count == 1
    await worker.close()
    _assert_fully_closed(worker)
    with pytest.raises(EpochWorkerError):
        worker.take_commit(commit_handle)


@pytest.mark.asyncio
async def test_clean_job_failure_close_settles_existing_waiter_then_clears_registry(monkeypatch, tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class Store:
        def read_candidate(self):
            entered.set()
            release.wait(timeout=2)
            raise EpochStoreError("backend detail")

        def close(self):
            return None

    monkeypatch.setattr("src.adapter.minecraft.epoch_worker.EpochStore.provision", lambda *a, **k: Store())
    worker = MinecraftEpochStoreWorker(
        EpochWorkerConfig(
            mode="PROVISION",
            directory=(tmp_path / "epoch").resolve(),
            platform=Platform(),
            provision_id=PROVISION,
            body_id=BODY,
        )
    )
    await worker.start()
    handle = worker.submit_preflight(preflight())
    waiter = asyncio.create_task(worker.wait(handle))
    assert entered.wait(timeout=2)
    release.set()
    with pytest.raises(EpochWorkerError, match="job failed"):
        await asyncio.wait_for(waiter, timeout=2)
    await worker.close()
    _assert_fully_closed(worker)
