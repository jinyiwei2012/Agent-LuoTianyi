"""Dedicated-thread lifecycle owner for the disabled-by-default epoch store."""

from __future__ import annotations

import asyncio
import queue
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal
from uuid import UUID

from .epoch_store import (
    CommittedEpoch,
    EpochCandidate,
    EpochCustodyPlatform,
    EpochOpenAttemptReceipt,
    EpochPlatform,
    EpochStore,
    EpochStoreError,
    TrustedEpochPolicy,
    VerifiedReadyContext,
)

_I63_MAX = (1 << 63) - 1
_CAPACITY = 2


class EpochWorkerError(RuntimeError):
    """Sanitized worker failure without filesystem, SQLite, or native details."""


class EpochWorkerState(str, Enum):
    NEW = "NEW"
    STARTING = "STARTING"
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    CLEANUP_UNKNOWN = "CLEANUP_UNKNOWN"


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class EpochWorkerConfig:
    mode: Literal["OPEN", "PROVISION", "OPEN_WITH_CUSTODY"]
    directory: Path | str
    platform: EpochPlatform | None = None
    provision_id: str | None = None
    body_id: str | None = None
    custody_policy: TrustedEpochPolicy | None = None
    custody: EpochCustodyPlatform | None = None

    def __post_init__(self) -> None:
        if self.mode == "OPEN_WITH_CUSTODY":
            if (
                type(self.custody_policy) is not TrustedEpochPolicy
                or not isinstance(self.custody, EpochCustodyPlatform)
                or self.platform is not None
                or self.provision_id is not None
                or self.body_id is not None
                or type(self.directory) is not str
                or self.directory != self.custody_policy.raw_root_path
            ):
                raise ValueError("invalid custody worker configuration")
            return
        if self.custody_policy is not None or self.custody is not None:
            raise ValueError("legacy mode rejects custody fields")
        path = Path(self.directory)
        if not path.is_absolute() or self.mode not in ("OPEN", "PROVISION"):
            raise ValueError("invalid epoch worker configuration")
        if self.mode == "OPEN":
            if self.provision_id is not None or self.body_id is not None:
                raise ValueError("open mode does not accept identities")
        elif not _uuid(self.provision_id) or not _uuid(self.body_id):
            raise ValueError("provision mode requires canonical identities")


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class EpochPreflightRequest:
    provision_id: str
    body_id: str
    coordinator_generation: int
    route_id: str
    server_connection_id: str
    controller_connection_id: str

    def __post_init__(self) -> None:
        if not _uuid(self.provision_id) or not _uuid(self.body_id):
            raise ValueError("invalid epoch worker identity")
        _correlation(
            self.coordinator_generation, self.route_id, self.server_connection_id, self.controller_connection_id
        )


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class EpochCommitRequest:
    preflight: "EpochPreflightSnapshot"
    evidence_id: str
    pending_identity: object
    coordinator_generation: int
    route_id: str
    server_connection_id: str
    controller_connection_id: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.preflight, EpochPreflightSnapshot)
            or type(self.evidence_id) is not str
            or not self.evidence_id
            or self.pending_identity is None
        ):
            raise ValueError("invalid epoch commit request")
        _correlation(
            self.coordinator_generation, self.route_id, self.server_connection_id, self.controller_connection_id
        )


class EpochJobHandle:
    __slots__ = ("_identity",)

    def __init__(self, identity: object | None = None) -> None:
        self._identity = identity

    def __repr__(self) -> str:
        return "EpochJobHandle(<opaque>)"

    def __copy__(self):
        return EpochJobHandle()

    def __deepcopy__(self, memo):
        return EpochJobHandle()


@dataclass(frozen=True, slots=True, repr=False)
class EpochPreflightSnapshot:
    provision_id: str
    body_id: str
    expected_last_epoch: int
    expected_generation: int
    next_epoch: int
    coordinator_generation: int
    route_id: str
    server_connection_id: str
    controller_connection_id: str
    _job_identity: object
    _store_identity: object


@dataclass(frozen=True, slots=True, repr=False)
class EpochCommitSnapshot:
    provision_id: str
    body_id: str
    last_epoch: int
    generation: int
    coordinator_generation: int
    route_id: str
    server_connection_id: str
    controller_connection_id: str
    pending_identity: object
    _job_identity: object
    _store_identity: object


@dataclass(slots=True)
class _OwnerJob:
    identity: object
    kind: str
    request: object
    future: asyncio.Future
    phase: str = "QUEUED"
    result: object = None
    candidate: EpochCandidate | None = None
    failed: bool = False


@dataclass(frozen=True, slots=True)
class _Command:
    identity: object
    kind: str
    request: object
    candidate: EpochCandidate | None = None


@dataclass(frozen=True, slots=True)
class _Completion:
    identity: object
    kind: str
    value: object = None
    failed: bool = False
    obsolete: bool = False


@dataclass(frozen=True, slots=True)
class _Terminal:
    startup: bool
    cleanup_unknown: bool
    retention: str = "NONE"
    transaction_unknown: bool | str = "NOT_REPORTED"


@dataclass(frozen=True, slots=True)
class EpochWorkerSnapshot:
    terminal_published: bool
    thread_completion_observed: bool
    cleanup: str
    retention: str
    transaction_unknown: bool | str
    notification: str


class MinecraftEpochStoreWorker:
    """One loop-owned facade over one non-daemon store-owning thread.

    Delivered preflight leases remain inside the same global capacity of two
    until exact atomic conversion to a commit job or explicit obsolescence.
    """

    def __init__(self, config: EpochWorkerConfig) -> None:
        if not isinstance(config, EpochWorkerConfig):
            raise TypeError("invalid epoch worker configuration")
        self._config = config
        self._state = EpochWorkerState.NEW
        self._loop = None
        self._owner_thread = None
        self._thread: threading.Thread | None = None
        self._commands: queue.Queue = queue.Queue(maxsize=3)
        self._jobs: dict[object, _OwnerJob] = {}
        self._store_identity = object()
        self._claim_lock = threading.Lock()
        self._claimed: set[object] = set()
        self._obsolete: set[object] = set()
        self._start_future = None
        self._close_future = None
        self._start_task = None
        self._close_task = None
        self._terminal_receipt: _Terminal | None = None
        self._terminal_lock = threading.Lock()
        self._terminal_notification = "UNKNOWN"
        self._retained_receipt = None
        self._retained_store = None
        self._fatal = None
        self._public_lifecycle = False

    def snapshot(self) -> EpochWorkerSnapshot:
        with self._terminal_lock:
            terminal = self._terminal_receipt
            notification = self._terminal_notification
        dead = self._thread is not None and not self._thread.is_alive()
        return EpochWorkerSnapshot(
            terminal is not None,
            dead,
            "NOT_YET_REPORTED" if terminal is None else "UNKNOWN" if terminal.cleanup_unknown else "CONFIRMED",
            terminal.retention if terminal else "NOT_YET_REPORTED",
            terminal.transaction_unknown if terminal else "NOT_REPORTED",
            notification,
        )

    def _deliver_fatal(self):
        if self._fatal is not None:
            raise self._fatal

    @property
    def state(self) -> EpochWorkerState:
        self._owner()
        return self._state

    @property
    def outstanding_count(self) -> int:
        self._owner()
        return len(self._jobs)

    async def start(self) -> None:
        if self._public_lifecycle:
            raise EpochWorkerError("concurrent lifecycle entry is unsupported")
        self._public_lifecycle = True
        try:
            await self._start_public()
        finally:
            self._public_lifecycle = False

    async def _start_public(self) -> None:
        self._bind_owner()
        if self._state is not EpochWorkerState.NEW:
            self._deliver_fatal()
            raise EpochWorkerError("epoch worker already started")
        self._state = EpochWorkerState.STARTING
        self._start_future = self._loop.create_future()
        self._thread = threading.Thread(target=self._thread_main, name="minecraft-epoch-store-owner", daemon=False)
        self._thread.start()
        self._start_task = asyncio.create_task(self._wait_start(), name="minecraft-epoch-worker-start")
        try:
            await asyncio.shield(self._start_task)
        except EpochWorkerError:
            self._deliver_fatal()
            raise
        except asyncio.CancelledError as cancellation:
            await self._drain_owned_task(self._start_task)
            await self._drain_owned_task(self._ensure_close_task())
            self._deliver_fatal()
            raise cancellation

    def submit_preflight(self, request: EpochPreflightRequest) -> EpochJobHandle:
        self._owner()
        if self._state is not EpochWorkerState.AVAILABLE or not isinstance(request, EpochPreflightRequest):
            raise EpochWorkerError("epoch worker is unavailable")
        if len(self._jobs) >= _CAPACITY:
            raise EpochWorkerError("epoch worker capacity")
        identity = object()
        job = _OwnerJob(identity, "PREFLIGHT", request, self._loop.create_future())
        self._jobs[identity] = job
        try:
            self._commands.put_nowait(_Command(identity, "PREFLIGHT", request))
        except queue.Full:
            self._jobs.pop(identity, None)
            raise EpochWorkerError("epoch worker queue unavailable") from None
        return EpochJobHandle(identity)

    def submit_commit(self, request: EpochCommitRequest) -> EpochJobHandle:
        self._owner()
        if self._state is not EpochWorkerState.AVAILABLE or type(request) is not EpochCommitRequest:
            raise EpochWorkerError("epoch worker is unavailable")
        snapshot = request.preflight
        if type(snapshot) is not EpochPreflightSnapshot:
            raise EpochWorkerError("preflight lease is not current")
        old = self._validated_preflight_job(
            snapshot,
            provision_id=snapshot.provision_id,
            body_id=snapshot.body_id,
            coordinator_generation=request.coordinator_generation,
            route_id=request.route_id,
            server_connection_id=request.server_connection_id,
            controller_connection_id=request.controller_connection_id,
        )
        if old is None:
            raise EpochWorkerError("preflight lease is not current")
        identity = object()
        job = _OwnerJob(identity, "COMMIT", request, self._loop.create_future(), candidate=old.candidate)
        self._jobs.pop(old.identity)
        self._jobs[identity] = job
        try:
            self._commands.put_nowait(_Command(identity, "COMMIT", request, old.candidate))
        except queue.Full:
            self._jobs.pop(identity, None)
            self._jobs[old.identity] = old
            raise EpochWorkerError("epoch worker queue unavailable") from None
        return EpochJobHandle(identity)

    def validates_preflight(
        self,
        snapshot: EpochPreflightSnapshot,
        *,
        provision_id: str,
        body_id: str,
        coordinator_generation: int,
        route_id: str,
        server_connection_id: str,
        controller_connection_id: str,
    ) -> bool:
        self._owner()
        if (
            type(snapshot) is not EpochPreflightSnapshot
            or not _uuid(provision_id)
            or not _uuid(body_id)
            or type(coordinator_generation) is not int
            or not 0 <= coordinator_generation <= _I63_MAX
            or not _uuid(route_id)
            or not _uuid(server_connection_id)
            or not _uuid(controller_connection_id)
        ):
            return False
        return (
            self._validated_preflight_job(
                snapshot,
                provision_id=provision_id,
                body_id=body_id,
                coordinator_generation=coordinator_generation,
                route_id=route_id,
                server_connection_id=server_connection_id,
                controller_connection_id=controller_connection_id,
            )
            is not None
        )

    async def wait(self, handle: EpochJobHandle) -> None:
        self._owner()
        job = self._job(handle)
        while not job.future.done():
            await asyncio.sleep(0.001)
            self._observe_terminal()
        await asyncio.shield(job.future)
        if job.failed:
            raise EpochWorkerError("epoch worker job failed")

    def take_preflight(self, handle: EpochJobHandle) -> EpochPreflightSnapshot:
        self._owner()
        job = self._job(handle)
        if job.kind != "PREFLIGHT" or job.phase != "COMPLETED" or not isinstance(job.result, EpochPreflightSnapshot):
            raise EpochWorkerError("preflight result is unavailable")
        job.phase = "DELIVERED"
        return job.result

    def take_commit(self, handle: EpochJobHandle) -> EpochCommitSnapshot:
        self._owner()
        job = self._job(handle)
        if job.kind != "COMMIT" or job.phase != "COMPLETED" or not isinstance(job.result, EpochCommitSnapshot):
            raise EpochWorkerError("commit result is unavailable")
        self._jobs.pop(job.identity)
        job.phase = "CONSUMED"
        return job.result

    def mark_obsolete(self, value: EpochJobHandle | EpochPreflightSnapshot) -> None:
        self._owner()
        identity = value._identity if isinstance(value, EpochJobHandle) else getattr(value, "_job_identity", None)
        job = self._jobs.get(identity)
        if job is None or (isinstance(value, EpochPreflightSnapshot) and job.result is not value):
            raise EpochWorkerError("epoch job is not current")
        with self._claim_lock:
            self._obsolete.add(identity)
            claimed = identity in self._claimed
        if not claimed and job.phase in {"COMPLETED", "DELIVERED"}:
            self._jobs.pop(identity, None)
            job.phase = "OBSOLETE"
            with self._claim_lock:
                self._obsolete.discard(identity)

    async def close(self) -> None:
        if self._public_lifecycle:
            raise EpochWorkerError("concurrent lifecycle entry is unsupported")
        self._public_lifecycle = True
        try:
            await self._close_public()
        finally:
            self._public_lifecycle = False

    async def _close_public(self) -> None:
        self._owner()
        task = self._ensure_close_task()
        cancellation = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                cancellation = cancellation or error
            except EpochWorkerError:
                break
        self._deliver_fatal()
        task.result()
        if cancellation is not None:
            raise cancellation

    async def _wait_start(self) -> None:
        while not self._start_future.done():
            self._observe_terminal()
            await asyncio.sleep(0.001)
        await self._wait_thread_dead_if_terminal()
        if self._state is not EpochWorkerState.AVAILABLE:
            raise EpochWorkerError("epoch worker is unavailable")

    def _ensure_close_task(self) -> asyncio.Task:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_operation(), name="minecraft-epoch-worker-close")
        return self._close_task

    async def _close_operation(self) -> None:
        if self._state is EpochWorkerState.CLOSED:
            return
        if self._state is EpochWorkerState.CLEANUP_UNKNOWN:
            raise EpochWorkerError("epoch worker cleanup outcome is unknown")
        if self._state is EpochWorkerState.UNAVAILABLE:
            await self._wait_thread_dead()
            if self._terminal_receipt is not None and self._terminal_receipt.cleanup_unknown:
                self._state = EpochWorkerState.CLEANUP_UNKNOWN
                raise EpochWorkerError("epoch worker cleanup outcome is unknown")
            self._finalize_closed()
            return
        if self._thread is None or not self._thread.is_alive():
            if self._state is EpochWorkerState.CLEANUP_UNKNOWN:
                raise EpochWorkerError("epoch worker cleanup outcome is unknown")
            self._finalize_closed()
            return
        if self._close_future is None:
            self._state = EpochWorkerState.CLOSING
            self._close_future = self._loop.create_future()
            for identity in self._jobs:
                with self._claim_lock:
                    self._obsolete.add(identity)
            self._commands.put_nowait(None)
        await self._wait_close_notification()
        await self._wait_thread_dead()
        if self._terminal_receipt is None or self._terminal_receipt.cleanup_unknown:
            self._state = EpochWorkerState.CLEANUP_UNKNOWN
            raise EpochWorkerError("epoch worker cleanup outcome is unknown")
        self._finalize_closed()

    async def _wait_close_notification(self):
        while not self._close_future.done():
            self._observe_terminal()
            await asyncio.sleep(0.001)

    async def _wait_thread_dead_if_terminal(self) -> None:
        if self._state in {EpochWorkerState.UNAVAILABLE, EpochWorkerState.CLEANUP_UNKNOWN}:
            await self._wait_thread_dead()
            self._settle_terminal_jobs()

    async def _wait_thread_dead(self) -> None:
        while self._thread is not None and self._thread.is_alive():
            await asyncio.sleep(0)
        if self._thread is not None:
            self._thread.join(0)
        self._observe_terminal()

    def _observe_terminal(self) -> None:
        with self._terminal_lock:
            terminal = self._terminal_receipt
        if terminal is not None:
            self._owner_terminal(terminal)

    async def _drain_owned_task(self, task: asyncio.Task) -> None:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if task.done():
            try:
                task.result()
            except Exception:
                pass

    def _thread_main(self) -> None:
        store = None
        startup = True
        terminal_unknown = False
        try:
            store = self._open_store()
            if not self._post(self._owner_started):
                raise EpochWorkerError("startup notification rejected")
            startup = False
            while True:
                command = self._commands.get()
                if command is None:
                    break
                if not self._claim(command.identity):
                    if not self._post(self._owner_complete, _Completion(command.identity, command.kind, obsolete=True)):
                        break
                    continue
                try:
                    completion = self._execute(store, command)
                finally:
                    self._release_claim(command.identity)
                if not self._post(self._owner_complete, completion):
                    break
                if completion.failed:
                    break
        except BaseException as error:
            self._remember_fatal(error)
            terminal_unknown = not isinstance(error, Exception) or _cleanup_unknown_error(error)
        terminal_unknown = self._close_store_thread(store, terminal_unknown)
        receipt = self._typed_terminal(store, startup, terminal_unknown)
        with self._terminal_lock:
            self._terminal_receipt = receipt
        accepted = self._post(self._owner_terminal, receipt)
        with self._terminal_lock:
            self._terminal_notification = "ACCEPTED" if accepted else "REJECTED"

    def _close_store_thread(self, store, unknown):
        if store is not None:
            try:
                store.close()
            except BaseException as error:
                self._remember_fatal(error)
                unknown = True
        return unknown

    def _remember_fatal(self, error):
        if not isinstance(error, Exception) and self._fatal is None:
            self._fatal = error

    def _typed_terminal(self, store, startup, unknown):
        retention = "NONE"
        transaction = "NOT_REPORTED"
        if self._config.mode == "OPEN_WITH_CUSTODY":
            try:
                evidence = store.cleanup_evidence() if store is not None else self._retained_receipt.snapshot()
                unknown = evidence.state not in {"RELEASED", "TRANSFERRED"} or evidence.retained_resources != 0
                transaction = evidence.transaction_unknown
            except BaseException as error:
                self._remember_fatal(error)
                unknown = True
            if unknown:
                retention = "STORE" if store is not None else "OPEN_RECEIPT"
            else:
                self._retained_store = None
                self._retained_receipt = None
        return _Terminal(startup, unknown, retention, transaction)

    def _open_store(self) -> EpochStore:
        if self._config.mode == "OPEN_WITH_CUSTODY":
            self._retained_receipt = EpochOpenAttemptReceipt()
            store = EpochStore.open_with_custody(
                self._config.custody_policy, custody=self._config.custody, receipt=self._retained_receipt
            )
            self._retained_store = store
            return store
        if self._config.mode == "OPEN":
            return EpochStore.open(self._config.directory, platform=self._config.platform)
        return EpochStore.provision(
            self._config.directory,
            self._config.provision_id,
            self._config.body_id,
            platform=self._config.platform,
        )

    def _claim(self, identity: object) -> bool:
        with self._claim_lock:
            if identity in self._obsolete:
                return False
            self._claimed.add(identity)
            return True

    def _release_claim(self, identity: object) -> None:
        with self._claim_lock:
            self._claimed.discard(identity)

    def _execute(self, store: EpochStore, command: _Command) -> _Completion:
        try:
            if command.kind == "PREFLIGHT":
                candidate = store.read_candidate()
                if (
                    candidate.provision_id != command.request.provision_id
                    or candidate.body_id != command.request.body_id
                ):
                    return _Completion(command.identity, command.kind, failed=True)
                return _Completion(command.identity, command.kind, candidate)
            request = command.request
            candidate = command.candidate
            committed = store.commit_ready(
                candidate,
                VerifiedReadyContext(
                    candidate.provision_id,
                    candidate.body_id,
                    candidate.expected_last_epoch,
                    candidate.expected_generation,
                    candidate.next_epoch,
                    request.evidence_id,
                ),
            )
            return _Completion(command.identity, command.kind, committed)
        except Exception:
            return _Completion(command.identity, command.kind, failed=True)

    def _post(self, callback, *args) -> bool:
        if self._loop is None or self._loop.is_closed():
            return False
        try:
            self._loop.call_soon_threadsafe(callback, *args)
            return True
        except BaseException:
            return False

    def _owner_started(self) -> None:
        if self._terminal_receipt is not None:
            return
        self._state = EpochWorkerState.AVAILABLE
        if self._start_future is not None and not self._start_future.done():
            self._start_future.set_result(None)

    def _owner_complete(self, event: _Completion) -> None:
        job = self._jobs.get(event.identity)
        if job is None:
            return
        with self._claim_lock:
            obsolete = event.identity in self._obsolete
            self._obsolete.discard(event.identity)
        if event.obsolete or obsolete:
            self._jobs.pop(event.identity, None)
            job.phase = "OBSOLETE"
        elif event.failed:
            job.failed = True
            job.phase = "FAILED"
            self._state = EpochWorkerState.UNAVAILABLE
        elif event.kind == "PREFLIGHT":
            candidate = event.value
            job.candidate = candidate
            job.result = EpochPreflightSnapshot(
                provision_id=candidate.provision_id,
                body_id=candidate.body_id,
                expected_last_epoch=candidate.expected_last_epoch,
                expected_generation=candidate.expected_generation,
                next_epoch=candidate.next_epoch,
                coordinator_generation=job.request.coordinator_generation,
                route_id=job.request.route_id,
                server_connection_id=job.request.server_connection_id,
                controller_connection_id=job.request.controller_connection_id,
                _job_identity=job.identity,
                _store_identity=self._store_identity,
            )
            job.phase = "COMPLETED"
        else:
            job.result = _commit_snapshot(event.value, job.request, job.identity, self._store_identity)
            job.phase = "COMPLETED"
        if not job.future.done():
            job.future.set_result(None)

    def _owner_terminal(self, receipt: _Terminal) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if receipt.cleanup_unknown:
            self._state = EpochWorkerState.CLEANUP_UNKNOWN
        elif receipt.startup:
            self._state = EpochWorkerState.UNAVAILABLE
        elif self._state is not EpochWorkerState.CLOSING:
            self._state = EpochWorkerState.UNAVAILABLE
        self._settle_terminal_jobs()
        if self._start_future is not None and not self._start_future.done():
            self._start_future.set_result(None)
        if self._close_future is not None and not self._close_future.done():
            self._close_future.set_result(None)

    def _settle_terminal_jobs(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        for job in self._jobs.values():
            if job.phase not in {"COMPLETED", "DELIVERED", "CONSUMED"}:
                job.failed = True
                job.phase = "FAILED"
            if not job.future.done():
                job.future.set_result(None)
        if self._thread is not None and not self._thread.is_alive():
            with self._claim_lock:
                self._claimed.clear()
                self._obsolete.clear()

    def _finalize_closed(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise EpochWorkerError("epoch worker cleanup outcome is unknown")
        if self._thread is not None and (self._terminal_receipt is None or self._terminal_receipt.cleanup_unknown):
            raise EpochWorkerError("epoch worker cleanup outcome is unknown")
        self._settle_terminal_jobs()
        for job in self._jobs.values():
            job.phase = "OBSOLETE"
            job.result = None
            job.candidate = None
        self._jobs.clear()
        with self._claim_lock:
            self._claimed.clear()
            self._obsolete.clear()
        self._state = EpochWorkerState.CLOSED

    def _job(self, handle: EpochJobHandle) -> _OwnerJob:
        if not isinstance(handle, EpochJobHandle):
            raise EpochWorkerError("invalid epoch job handle")
        job = self._jobs.get(handle._identity)
        if job is None:
            raise EpochWorkerError("epoch job is not current")
        return job

    def _validated_preflight_job(
        self,
        snapshot: EpochPreflightSnapshot,
        *,
        provision_id: str,
        body_id: str,
        coordinator_generation: int,
        route_id: str,
        server_connection_id: str,
        controller_connection_id: str,
    ) -> _OwnerJob | None:
        if self._state is not EpochWorkerState.AVAILABLE or type(snapshot) is not EpochPreflightSnapshot:
            return None
        job = next(
            (
                candidate
                for candidate in self._jobs.values()
                if candidate.kind == "PREFLIGHT" and candidate.result is snapshot
            ),
            None,
        )
        if (
            job is None
            or job.phase != "DELIVERED"
            or job.failed
            or snapshot._job_identity is not job.identity
            or snapshot._store_identity is not self._store_identity
            or snapshot.provision_id != provision_id
            or snapshot.body_id != body_id
            or snapshot.coordinator_generation != coordinator_generation
            or snapshot.route_id != route_id
            or snapshot.server_connection_id != server_connection_id
            or snapshot.controller_connection_id != controller_connection_id
        ):
            return None
        with self._claim_lock:
            if job.identity in self._obsolete:
                return None
        return job

    def _bind_owner(self) -> None:
        loop = asyncio.get_running_loop()
        thread = threading.get_ident()
        if self._loop is None:
            self._loop, self._owner_thread = loop, thread
        elif self._loop is not loop or self._owner_thread != thread:
            raise EpochWorkerError("wrong epoch worker owner")

    def _owner(self) -> None:
        if self._loop is None:
            raise EpochWorkerError("epoch worker is not started")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as error:
            raise EpochWorkerError("wrong epoch worker owner") from error
        if loop is not self._loop or threading.get_ident() != self._owner_thread:
            raise EpochWorkerError("wrong epoch worker owner")


def _uuid(value: object) -> bool:
    try:
        return type(value) is str and str(UUID(value)) == value and UUID(value).int != 0
    except (TypeError, ValueError):
        return False


def _correlation(generation: int, route: str, server: str, controller: str) -> None:
    if (
        type(generation) is not int
        or not 0 <= generation <= _I63_MAX
        or not all(_uuid(value) for value in (route, server, controller))
    ):
        raise ValueError("invalid epoch worker correlation")


def _request_matches_snapshot(request: EpochCommitRequest, snapshot: EpochPreflightSnapshot) -> bool:
    return (
        request.coordinator_generation == snapshot.coordinator_generation
        and request.route_id == snapshot.route_id
        and request.server_connection_id == snapshot.server_connection_id
        and request.controller_connection_id == snapshot.controller_connection_id
    )


def _commit_snapshot(
    committed: CommittedEpoch,
    request: EpochCommitRequest,
    identity: object,
    store_identity: object,
) -> EpochCommitSnapshot:
    return EpochCommitSnapshot(
        provision_id=committed.provision_id,
        body_id=committed.body_id,
        last_epoch=committed.last_epoch,
        generation=committed.generation,
        coordinator_generation=request.coordinator_generation,
        route_id=request.route_id,
        server_connection_id=request.server_connection_id,
        controller_connection_id=request.controller_connection_id,
        pending_identity=request.pending_identity,
        _job_identity=identity,
        _store_identity=store_identity,
    )


def _cleanup_unknown_error(error: BaseException) -> bool:
    return isinstance(error, EpochStoreError) and "cleanup outcome is unknown" in str(error)
