import os
import threading

import pytest

from src.adapter.minecraft import epoch_store
from src.adapter.minecraft.epoch_platform import WindowsEpochPlatform
from src.adapter.minecraft.epoch_store import EpochStore, PlatformCapability
from src.adapter.minecraft.epoch_worker import (
    EpochCommitRequest,
    EpochPreflightRequest,
    EpochWorkerConfig,
    EpochWorkerError,
    EpochWorkerState,
    MinecraftEpochStoreWorker,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="requires real Win32 no-share lock")

PROVISION = "10000000-0000-4000-8000-000000000001"
BODY = "20000000-0000-4000-8000-000000000002"
ROUTE = "30000000-0000-4000-8000-000000000003"
SERVER = "40000000-0000-4000-8000-000000000004"
CLIENT = "50000000-0000-4000-8000-000000000005"


class _Connection:
    def __init__(self, value, events):
        self._value = value
        self._events = events

    def execute(self, *args, **kwargs):
        return self._value.execute(*args, **kwargs)

    def close(self):
        self._events.append(("sqlite_close", threading.get_ident()))
        return self._value.close()


class _Lock:
    def __init__(self, value, events):
        self._value = value
        self._events = events

    def close(self):
        self._events.append(("native_lock_close", threading.get_ident()))
        return self._value.close()


class _LockOnlyVerifiedPlatform:
    """TEST ONLY: real Win32 lock, not production ACL or durability evidence."""

    def __init__(self, events):
        self._native = WindowsEpochPlatform()
        self._events = events

    def capability(self, directory):
        self._events.append(("capability", threading.get_ident()))
        facts = self._native.volume_facts(directory.parent if not directory.exists() else directory)
        if not facts.local or not facts.persistent_acls or facts.reparse_point:
            return PlatformCapability(False, "LOCK_ONLY_NOT_PRODUCTION_CAPABILITY")
        return PlatformCapability(True, "LOCK_ONLY_NOT_PRODUCTION_CAPABILITY")

    def acquire_lock(self, path):
        self._events.append(("native_lock_acquire", threading.get_ident()))
        return _Lock(self._native.acquire_lock(path), self._events)


def _preflight():
    return EpochPreflightRequest(
        provision_id=PROVISION,
        body_id=BODY,
        coordinator_generation=1,
        route_id=ROUTE,
        server_connection_id=SERVER,
        controller_connection_id=CLIENT,
    )


def _instrument_real_store(monkeypatch, events):
    original_provision = EpochStore.provision.__func__
    original_open = EpochStore.open.__func__
    original_read = EpochStore.read_candidate
    original_commit = EpochStore.commit_ready
    original_readback = EpochStore._read_row
    original_close = EpochStore.close
    original_create = epoch_store._connect_create
    original_rw = epoch_store._connect_rw

    monkeypatch.setattr(
        EpochStore,
        "provision",
        classmethod(
            lambda cls, *args, **kwargs: events.append(("provision", threading.get_ident()))
            or original_provision(cls, *args, **kwargs)
        ),
    )
    monkeypatch.setattr(
        EpochStore,
        "open",
        classmethod(
            lambda cls, *args, **kwargs: events.append(("open", threading.get_ident()))
            or original_open(cls, *args, **kwargs)
        ),
    )
    monkeypatch.setattr(
        EpochStore,
        "read_candidate",
        lambda self: events.append(("read_candidate", threading.get_ident())) or original_read(self),
    )
    monkeypatch.setattr(
        EpochStore,
        "commit_ready",
        lambda self, candidate, ready: events.append(("commit_ready", threading.get_ident()))
        or original_commit(self, candidate, ready),
    )
    monkeypatch.setattr(
        EpochStore,
        "_read_row",
        lambda self: events.append(("readback", threading.get_ident())) or original_readback(self),
    )
    monkeypatch.setattr(
        EpochStore,
        "close",
        lambda self: events.append(("store_close", threading.get_ident())) or original_close(self),
    )
    monkeypatch.setattr(
        epoch_store,
        "_connect_create",
        lambda path: _Connection(original_create(path), events),
    )
    monkeypatch.setattr(
        epoch_store,
        "_connect_rw",
        lambda path: _Connection(original_rw(path), events),
    )


@pytest.mark.asyncio
async def test_native_lock_exclusion_persisted_epoch_and_reacquisition(monkeypatch, tmp_path):
    events = []
    _instrument_real_store(monkeypatch, events)
    root = (tmp_path / "epoch").resolve()
    owner_thread = threading.get_ident()
    first = MinecraftEpochStoreWorker(
        EpochWorkerConfig(
            mode="PROVISION",
            directory=root,
            platform=_LockOnlyVerifiedPlatform(events),
            provision_id=PROVISION,
            body_id=BODY,
        )
    )
    second = None
    third = None
    try:
        await first.start()
        first_thread = first._thread
        preflight = first.submit_preflight(_preflight())
        await first.wait(preflight)
        snapshot = first.take_preflight(preflight)
        pending_identity = object()
        commit = first.submit_commit(
            EpochCommitRequest(
                preflight=snapshot,
                evidence_id="test-only-ready-correlation",
                pending_identity=pending_identity,
                coordinator_generation=1,
                route_id=ROUTE,
                server_connection_id=SERVER,
                controller_connection_id=CLIENT,
            )
        )
        await first.wait(commit)
        committed = first.take_commit(commit)
        assert committed.pending_identity is pending_identity
        assert (committed.last_epoch, committed.generation) == (1, 1)

        second = MinecraftEpochStoreWorker(
            EpochWorkerConfig(mode="OPEN", directory=root, platform=_LockOnlyVerifiedPlatform(events))
        )
        with pytest.raises(EpochWorkerError, match="unavailable"):
            await second.start()
        assert second.state is EpochWorkerState.UNAVAILABLE
        second_thread = second._thread
        assert second_thread is not None and not second_thread.is_alive()
        assert first_thread is not None and first_thread.is_alive()
        assert first_thread.ident != second_thread.ident
        assert sum(name == "native_lock_acquire" for name, _ in events) == 2

        await first.close()
        assert first.state is EpochWorkerState.CLOSED and not first_thread.is_alive()

        third = MinecraftEpochStoreWorker(
            EpochWorkerConfig(mode="OPEN", directory=root, platform=_LockOnlyVerifiedPlatform(events))
        )
        await third.start()
        reopened = third.submit_preflight(_preflight())
        await third.wait(reopened)
        reopened_snapshot = third.take_preflight(reopened)
        assert (reopened_snapshot.expected_last_epoch, reopened_snapshot.expected_generation) == (1, 1)
        assert reopened_snapshot.next_epoch == 2
        third.mark_obsolete(reopened_snapshot)
        await third.close()

        first_ids = {
            thread_id
            for name, thread_id in events
            if name
            in {
                "provision",
                "capability",
                "native_lock_acquire",
                "read_candidate",
                "commit_ready",
                "readback",
                "sqlite_close",
                "store_close",
                "native_lock_close",
            }
            and thread_id == first_thread.ident
        }
        assert first_ids == {first_thread.ident} and owner_thread not in first_ids
        first_names = [name for name, thread_id in events if thread_id == first_thread.ident]
        for required in (
            "provision",
            "capability",
            "native_lock_acquire",
            "read_candidate",
            "commit_ready",
            "readback",
            "sqlite_close",
            "store_close",
            "native_lock_close",
        ):
            assert required in first_names
        assert sum(name == "native_lock_close" for name, thread_id in events if thread_id == first_thread.ident) == 1
        assert WindowsEpochPlatform().capability(root).verified is False
    finally:
        for worker in (third, second, first):
            if worker is None or worker._loop is None:
                continue
            try:
                await worker.close()
            except EpochWorkerError:
                pass
            assert worker._thread is None or not worker._thread.is_alive()
