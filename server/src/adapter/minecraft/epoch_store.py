"""Offline, single-body epoch counter backed by an independent SQLite database.

The default platform capability is deliberately disabled: SQLite transactional
semantics alone do not verify this host's ACL, VFS, lock, or power-loss claims.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import struct
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import quote
from uuid import UUID

_I63_MAX = (1 << 63) - 1
_DOMAIN = b"luosandbox.epoch-row.v1"
_SCHEMA = """CREATE TABLE epoch_row (
version INTEGER NOT NULL, provision_id TEXT NOT NULL, body_id TEXT NOT NULL,
last_epoch INTEGER NOT NULL, generation INTEGER NOT NULL, record_hash BLOB NOT NULL)"""
_SCHEMA_NORMALIZED = " ".join(_SCHEMA.split())


class EpochStoreError(RuntimeError):
    """A fail-closed epoch-store error with no raw filesystem or SQLite detail."""


@dataclass(frozen=True, slots=True)
class EpochCleanupEvidence:
    state: str
    reasons: tuple[str, ...]
    retained_resources: int
    transaction_unknown: bool


class EpochOpenCleanupError(EpochStoreError):
    def __init__(self, evidence: EpochCleanupEvidence):
        super().__init__("epoch store cleanup outcome is unknown")
        self.evidence = evidence


@dataclass(frozen=True, slots=True, repr=False)
class TrustedEpochPolicy:
    raw_root_path: str
    expected_volume_serial: int
    expected_file_index: int
    expected_owner_sid: bytes
    content_writer_sids: tuple[bytes, ...]
    security_mutator_sids: tuple[bytes, ...]
    namespace_mutator_sids: tuple[bytes, ...]
    provision_id: str
    body_id: str

    def __post_init__(self) -> None:
        _raw_epoch_path(self.raw_root_path)
        for value, maximum in (
            (self.expected_volume_serial, 0xFFFFFFFF),
            (self.expected_file_index, 0xFFFFFFFFFFFFFFFF),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise EpochStoreError("invalid epoch root identity")
        # Public constructor validates SID bytes, without merging the three authority sets.
        from src.adapter.minecraft.deployment_acl import DeploymentAclError, HandleSecurityDescriptor

        try:
            HandleSecurityDescriptor(self.expected_owner_sid, True, ())
            for sids in (self.content_writer_sids, self.security_mutator_sids, self.namespace_mutator_sids):
                if type(sids) is not tuple or not 1 <= len(sids) <= 16:
                    raise EpochStoreError("invalid epoch mutation policy")
                for sid in sids:
                    HandleSecurityDescriptor(sid, True, ())
                if len(set(sids)) != len(sids):
                    raise EpochStoreError("duplicate epoch mutation identity")
        except DeploymentAclError:
            raise EpochStoreError("invalid epoch SID policy") from None
        if self.expected_owner_sid not in self.security_mutator_sids:
            raise EpochStoreError("epoch owner security authority is not trusted")
        _identities(self.provision_id, self.body_id)


def _raw_epoch_path(value: str) -> None:
    if type(value) is not str or len(value) > 1024 or not re.fullmatch(r"[A-Za-z]:\\[^/]+", value):
        raise EpochStoreError("invalid raw epoch path")
    for part in value[3:].split("\\"):
        if (
            not part
            or part in {".", ".."}
            or part[-1] in " ."
            or any(ord(char) < 32 or char in '<>:"/|?*' for char in part)
            or part.split(".")[0].upper()
            in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}
        ):
            raise EpochStoreError("invalid raw epoch path")


@runtime_checkable
class EpochOpenLease(Protocol):
    def read_marker(self) -> bytes: ...
    def validate(self) -> None: ...
    def close(self) -> None: ...


@runtime_checkable
class EpochCustodyPlatform(Protocol):
    def acquire(self, policy: TrustedEpochPolicy, receipt: EpochOpenAttemptReceipt) -> EpochOpenLease: ...


def _prefer(first: BaseException | None, later: BaseException) -> BaseException:
    return later if first is None or isinstance(first, Exception) and not isinstance(later, Exception) else first


class _CustodyResources:
    def __init__(self):
        self.leases: list[EpochOpenLease] = []
        self.connection = None
        self.connection_close_attempted = False
        self.lease_close_attempted: set[int] = set()
        self.unknown = False
        self.closing = False
        self.transaction_unknown = False
        self.operation = False
        self.acquiring = False
        self.reasons: list[str] = []

    def mark_unknown(self, reason: str) -> None:
        self.unknown = True
        if reason not in self.reasons and len(self.reasons) < 8:
            self.reasons.append(reason)

    def evidence(self, state: str) -> EpochCleanupEvidence:
        return EpochCleanupEvidence(
            state, tuple(self.reasons), len(self.leases) + (self.connection is not None), self.transaction_unknown
        )

    def close_connection(self) -> BaseException | None:
        if self.connection is None:
            return None
        if self.connection_close_attempted:
            self.mark_unknown("CONNECTION_CLOSE_UNKNOWN")
            return EpochOpenCleanupError(self.evidence("UNKNOWN"))
        self.connection_close_attempted = True
        try:
            self.connection.close()
        except BaseException as error:
            self.mark_unknown("CONNECTION_CLOSE_UNKNOWN")
            return error
        if self.unknown:
            return EpochOpenCleanupError(self.evidence("UNKNOWN"))
        self.connection = None
        self.connection_close_attempted = False
        return None

    def release(self) -> BaseException | None:
        self.acquiring = False
        if self.unknown or self.closing:
            self.mark_unknown("REENTRANT_OR_UNKNOWN_CLEANUP")
            return EpochOpenCleanupError(self.evidence("UNKNOWN"))
        self.closing = True
        first = self.close_connection()
        if self.connection is None:
            for lease in reversed(tuple(self.leases)):
                identity = id(lease)
                if identity in self.lease_close_attempted:
                    self.mark_unknown("LEASE_CLOSE_UNKNOWN")
                    continue
                self.lease_close_attempted.add(identity)
                try:
                    lease.close()
                except BaseException as error:
                    self.mark_unknown("LEASE_CLOSE_UNKNOWN")
                    first = _prefer(first, error)
                else:
                    if not self.unknown:
                        self.leases.remove(lease)
        self.closing = False
        if self.leases or self.connection is not None:
            self.mark_unknown("RETAINED_CLEANUP_RESOURCES")
        return first


class EpochOpenAttemptReceipt:
    """Caller must retain UNKNOWN receipts; no retry/reset/destructor or public resource accessor."""

    def __init__(self):
        self.__state = "FRESH"
        self.__resources = _CustodyResources()
        self.__lock = threading.RLock()
        self.__completion = None

    def snapshot(self) -> EpochCleanupEvidence:
        with self.__lock:
            return self.__resources.evidence(self.__state)

    def _claim(self, completion: object) -> _CustodyResources:
        with self.__lock:
            if self.__state != "FRESH":
                raise EpochStoreError("epoch open receipt is single use")
            self.__state = "IN_PROGRESS"
            self.__completion = completion
            self.__resources.acquiring = True
            return self.__resources

    def _registration_window(self) -> None:
        """Trusted port checks BEFORE acquisition, then registers before its next fallible boundary."""
        with self.__lock:
            resources = self.__resources
            if self.__state != "IN_PROGRESS" or not resources.acquiring or resources.closing or resources.unknown:
                raise EpochStoreError("custody acquisition window is closed")

    def _register(self, lease: EpochOpenLease) -> None:
        """Trusted acquisition port must register each partial resource before its next boundary."""
        with self.__lock:
            self._registration_window()
            if self.__state != "IN_PROGRESS" or not isinstance(lease, EpochOpenLease):
                raise EpochStoreError("invalid custody registration")
            if any(item is lease for item in self.__resources.leases):
                return
            if len(self.__resources.leases) >= 16:
                raise EpochStoreError("custody resource limit")
            self.__resources.leases.append(lease)

    def _finish(self, state: str, completion: object) -> None:
        with self.__lock:
            if self.__state != "IN_PROGRESS" or completion is not self.__completion:
                raise EpochStoreError("invalid custody completion")
            self.__resources.acquiring = False
            if state == "RELEASED" and (
                self.__resources.unknown or self.__resources.leases or self.__resources.connection is not None
            ):
                self.__resources.mark_unknown("RETAINED_CLEANUP_RESOURCES")
                state = "UNKNOWN"
            self.__state = state
            self.__completion = None
            if state == "TRANSFERRED":
                self.__resources = _CustodyResources()


@dataclass(frozen=True, slots=True)
class PlatformCapability:
    """Externally verified platform evidence; production defaults to unsupported."""

    verified: bool
    reason: str


class LockHandle(Protocol):
    def close(self) -> None: ...


@runtime_checkable
class EpochPlatform(Protocol):
    def capability(self, directory: Path) -> PlatformCapability: ...

    def acquire_lock(self, lock_path: Path) -> LockHandle: ...


class _UnsupportedPlatform:
    def capability(self, directory: Path) -> PlatformCapability:
        return PlatformCapability(False, "platform ACL and lock evidence is unavailable")

    def acquire_lock(self, lock_path: Path) -> LockHandle:
        raise EpochStoreError("platform lock is unavailable")


@dataclass(frozen=True, slots=True)
class EpochCandidate:
    provision_id: str
    body_id: str
    expected_last_epoch: int
    expected_generation: int
    next_epoch: int

    def __post_init__(self) -> None:
        _candidate_values(self.expected_last_epoch, self.expected_generation, self.next_epoch)
        _identities(self.provision_id, self.body_id)


@dataclass(frozen=True, slots=True)
class VerifiedReadyContext:
    """Registry-owned immutable evidence, not a network ``ready`` boolean."""

    provision_id: str
    body_id: str
    expected_last_epoch: int
    expected_generation: int
    next_epoch: int
    evidence_id: str

    def __post_init__(self) -> None:
        _candidate_values(self.expected_last_epoch, self.expected_generation, self.next_epoch)
        _identities(self.provision_id, self.body_id)


@dataclass(frozen=True, slots=True)
class CommittedEpoch:
    provision_id: str
    body_id: str
    last_epoch: int
    generation: int


class EpochStore:
    """A disabled-by-default, lifecycle-locked SQLite epoch counter.

    Callers must keep their verified READY evidence separate from payload parsing;
    this class matches it to a candidate but cannot prove game quiescence itself.
    """

    def __init__(self, directory: Path, platform: EpochPlatform, lock: LockHandle, identity: tuple[str, str]):
        self._directory = directory
        self._platform = platform
        self._lock = lock
        self._identity = identity
        self._disabled = False
        self._closed = False
        self._resource_released = False
        self._cleanup_unknown = False
        self._close_in_progress = False
        self._operation_lock = threading.RLock()
        self._custody_policy: TrustedEpochPolicy | None = None
        self._custody_resources: _CustodyResources | None = None

    @classmethod
    def open_with_custody(
        cls, policy: TrustedEpochPolicy, *, custody: EpochCustodyPlatform, receipt: EpochOpenAttemptReceipt
    ) -> "EpochStore":
        if type(receipt) is not EpochOpenAttemptReceipt:
            raise EpochStoreError("invalid epoch open receipt")
        completion = object()
        resources = receipt._claim(completion)
        try:
            if type(policy) is not TrustedEpochPolicy or not isinstance(custody, EpochCustodyPlatform):
                raise EpochStoreError("invalid epoch custody input")
            policy.__post_init__()
            lease = custody.acquire(policy, receipt)
            resources.acquiring = False
            if not any(item is lease for item in resources.leases):
                raise EpochStoreError("custody lease was not registered")
            store = cls(Path(policy.raw_root_path), custody, lease, (policy.provision_id, policy.body_id))
            store._custody_policy = policy
            store._custody_resources = resources
            with store._operation_lock:
                store._custody_operation(store._custody_read)
            receipt._finish("TRANSFERRED", completion)
            return store
        except BaseException as original:
            cleanup = resources.release()
            if cleanup is not None:
                original = _prefer(original, cleanup)
            receipt._finish("UNKNOWN" if resources.unknown else "RELEASED", completion)
            if not isinstance(original, Exception):
                raise original
            if resources.unknown:
                raise EpochOpenCleanupError(receipt.snapshot()) from None
            if isinstance(original, EpochStoreError):
                raise original
            raise EpochStoreError("epoch custody open failed") from None

    def cleanup_evidence(self) -> EpochCleanupEvidence:
        with self._operation_lock:
            if self._custody_resources is None:
                raise EpochStoreError("legacy store has no custody receipt")
            return self._custody_resources.evidence(
                "UNKNOWN"
                if self._custody_resources.unknown
                else "RELEASED" if self._resource_released else "TRANSFERRED"
            )

    def _custody_check(self) -> None:
        resources = self._custody_resources
        if self._closed or resources.unknown:
            raise EpochOpenCleanupError(resources.evidence("UNKNOWN"))
        self._lock.validate()
        if self._disabled or self._closed or resources.unknown:
            raise EpochOpenCleanupError(resources.evidence("UNKNOWN"))
        if _marker_bytes(self._lock.read_marker()) != self._identity:
            raise EpochStoreError("custody marker identity mismatch")
        if self._disabled or self._closed or resources.unknown:
            raise EpochOpenCleanupError(resources.evidence("UNKNOWN"))

    def _custody_connect(self):
        resources = self._custody_resources
        if resources.connection is not None or resources.unknown:
            raise EpochOpenCleanupError(resources.evidence("UNKNOWN"))
        self._custody_check()
        resources.connection = _connect_rw(self._directory / "epochs.sqlite3")
        resources.connection_close_attempted = False
        self._custody_check()
        _configure(resources.connection)
        return resources.connection

    def _custody_raise(self, original: BaseException, *, transaction: bool = False) -> None:
        self._disabled = True
        resources = self._custody_resources
        resources.transaction_unknown |= transaction
        cleanup = resources.close_connection() if not resources.unknown else None
        if cleanup is not None:
            original = _prefer(original, cleanup)
        if not isinstance(original, Exception):
            raise original
        if resources.unknown:
            raise EpochOpenCleanupError(resources.evidence("UNKNOWN")) from None
        raise EpochStoreError(
            "epoch commit outcome is unknown" if transaction else "epoch custody operation failed"
        ) from None

    def _custody_read(self) -> "_Row":
        try:
            connection = self._custody_connect()
            self._custody_check()
            row = _read_row(connection, self._identity)
            self._custody_check()
            failure = self._custody_resources.close_connection()
            if failure is not None:
                raise failure
            self._custody_check()
            return row
        except BaseException as original:
            self._custody_raise(original)

    def _custody_operation(self, action):
        resources = self._custody_resources
        if resources.operation:
            self._disabled = True
            resources.mark_unknown("REENTRANT_CUSTODY_OPERATION")
            raise EpochOpenCleanupError(resources.evidence("UNKNOWN"))
        token = object()
        resources.operation = token
        try:
            return action()
        finally:
            if resources.operation is token:
                resources.operation = False

    def _custody_commit(self, candidate: EpochCandidate, ready: VerifiedReadyContext) -> CommittedEpoch:
        attempted = False
        began = False
        try:
            _validate_candidate(candidate)
            _validate_ready(ready)
            self._custody_identity(candidate)
            if candidate != EpochCandidate(
                ready.provision_id,
                ready.body_id,
                ready.expected_last_epoch,
                ready.expected_generation,
                ready.next_epoch,
            ):
                raise EpochStoreError("ready evidence does not match candidate")
            connection = self._custody_connect()
            self._custody_check()
            connection.execute("BEGIN IMMEDIATE")
            began = True
            self._custody_check()
            current = _read_row(connection, self._identity)
            if (current.last_epoch, current.generation) != (
                candidate.expected_last_epoch,
                candidate.expected_generation,
            ):
                raise EpochStoreError("stale epoch candidate")
            generation = current.generation + 1
            if generation > _I63_MAX:
                raise EpochStoreError("epoch counter exhausted")
            self._custody_check()
            cursor = connection.execute(
                "UPDATE epoch_row SET last_epoch=?, generation=?, record_hash=? "
                "WHERE version=1 AND provision_id=? AND body_id=? AND last_epoch=? AND generation=? AND record_hash=?",
                (
                    candidate.next_epoch,
                    generation,
                    _row_hash(1, *self._identity, candidate.next_epoch, generation),
                    *self._identity,
                    current.last_epoch,
                    current.generation,
                    current.record_hash,
                ),
            )
            if cursor.rowcount != 1:
                raise EpochStoreError("epoch compare-and-set failed")
            self._custody_check()
            attempted = True
            connection.execute("COMMIT")
            self._custody_check()
            failure = self._custody_resources.close_connection()
            if failure is not None:
                raise failure
            committed = self._custody_read()
            if (committed.last_epoch, committed.generation) != (candidate.next_epoch, generation):
                raise EpochStoreError("epoch commit readback failed")
            return CommittedEpoch(*self._identity, committed.last_epoch, committed.generation)
        except BaseException as original:
            self._disabled = True
            if (
                began
                and not attempted
                and not self._custody_resources.unknown
                and self._custody_resources.connection is not None
            ):
                try:
                    self._custody_resources.connection.execute("ROLLBACK")
                except BaseException as rollback:
                    original = _prefer(original, rollback)
            self._custody_raise(original, transaction=attempted)

    def _custody_identity(self, candidate: EpochCandidate) -> None:
        if (candidate.provision_id, candidate.body_id) != self._identity:
            raise EpochStoreError("candidate identity mismatch")

    def _custody_close(self) -> None:
        resources = self._custody_resources
        if resources.operation:
            self._closed = self._disabled = True
            resources.mark_unknown("REENTRANT_OPERATION_CLOSE")
            raise EpochOpenCleanupError(resources.evidence("UNKNOWN"))
        if self._resource_released:
            return
        self._closed = self._disabled = True
        failure = resources.release()
        if failure is not None and not isinstance(failure, Exception):
            raise failure
        if resources.unknown:
            raise EpochOpenCleanupError(resources.evidence("UNKNOWN"))
        self._resource_released = True

    @classmethod
    def provision(
        cls, directory: str | Path, provision_id: str, body_id: str, *, platform: EpochPlatform | None = None
    ) -> "EpochStore":
        if not isinstance(directory, (str, Path)) or platform is not None and not isinstance(platform, EpochPlatform):
            raise EpochStoreError("invalid legacy epoch provision input")
        root = _directory(directory)
        provision_id, body_id = _identities(provision_id, body_id)
        port = platform or _UnsupportedPlatform()
        _require_capability(port, root)
        if root.exists() and any(root.iterdir()):
            raise EpochStoreError("installation is not empty")
        root.mkdir(parents=True, exist_ok=False)
        lock_path = root / "epochs.lock"
        lock: LockHandle | None = None
        try:
            lock_path.touch(exist_ok=False)
            lock = port.acquire_lock(lock_path)
            marker = {"bodyId": body_id, "provisionId": provision_id, "version": 1}
            (root / "provision.json").write_bytes(
                json.dumps(marker, separators=(",", ":"), sort_keys=True).encode("utf-8")
            )
            connection = _connect_create(root / "epochs.sqlite3")
            try:
                _configure(connection)
                connection.execute(_SCHEMA)
                record_hash = _row_hash(1, provision_id, body_id, 0, 0)
                connection.execute(
                    "INSERT INTO epoch_row VALUES (?, ?, ?, ?, ?, ?)", (1, provision_id, body_id, 0, 0, record_hash)
                )
            finally:
                connection.close()
            return cls(root, port, lock, (provision_id, body_id))
        except Exception as error:
            cleanup_failed = False
            if lock is not None:
                try:
                    lock.close()
                except Exception:
                    cleanup_failed = True
            if cleanup_failed:
                raise EpochStoreError("store provision cleanup outcome is unknown") from error
            if isinstance(error, EpochStoreError):
                raise
            raise EpochStoreError("provision failed") from error

    @classmethod
    def open(cls, directory: str | Path, *, platform: EpochPlatform | None = None) -> "EpochStore":
        root = _directory(directory)
        port = platform or _UnsupportedPlatform()
        _require_capability(port, root)
        db, marker, lock_path = root / "epochs.sqlite3", root / "provision.json", root / "epochs.lock"
        if not root.is_dir() or not db.is_file() or not marker.is_file() or not lock_path.is_file():
            raise EpochStoreError("epoch installation is incomplete")
        identity = _marker_identity(marker)
        lock = port.acquire_lock(lock_path)
        store = cls(root, port, lock, identity)
        try:
            store._read_row()
        except Exception as error:
            try:
                lock.close()
            except Exception:
                raise EpochStoreError("store open cleanup outcome is unknown") from error
            raise
        return store

    def close(self) -> None:
        with self._operation_lock:
            if self._custody_resources is not None:
                self._custody_close()
                return
            if self._resource_released:
                return
            if self._cleanup_unknown or self._close_in_progress:
                self._cleanup_unknown = True
                raise EpochStoreError("epoch store cleanup outcome is unknown")
            # Operational authority is permanently removed before cleanup starts.
            self._closed = True
            self._close_in_progress = True
            try:
                self._lock.close()
            except BaseException as error:
                self._close_in_progress = False
                self._cleanup_unknown = True
                if not isinstance(error, Exception):
                    raise
                raise EpochStoreError("epoch store cleanup outcome is unknown") from error
            if self._cleanup_unknown or not self._close_in_progress:
                self._close_in_progress = False
                self._cleanup_unknown = True
                raise EpochStoreError("epoch store cleanup outcome is unknown")
            self._close_in_progress = False
            self._resource_released = True

    def read_candidate(self) -> EpochCandidate:
        with self._operation_lock:
            self._ensure_enabled()
            row = self._read_row()
            if row.last_epoch >= _I63_MAX or row.generation >= _I63_MAX:
                self._disabled = True
                raise EpochStoreError("epoch counter exhausted")
            return EpochCandidate(row.provision_id, row.body_id, row.last_epoch, row.generation, row.last_epoch + 1)

    def commit_ready(self, candidate: EpochCandidate, ready: VerifiedReadyContext) -> CommittedEpoch:
        with self._operation_lock:
            self._ensure_enabled()
            if self._custody_resources is not None:
                return self._custody_operation(lambda: self._custody_commit(candidate, ready))
            return self._commit_ready(candidate, ready)

    def _commit_ready(self, candidate: EpochCandidate, ready: VerifiedReadyContext) -> CommittedEpoch:  # noqa: C901
        if not isinstance(candidate, EpochCandidate) or not isinstance(ready, VerifiedReadyContext):
            self._disabled = True
            raise EpochStoreError("invalid ready evidence")
        try:
            _validate_candidate(candidate)
            _validate_ready(ready)
        except (EpochStoreError, ValueError):
            self._disabled = True
            raise EpochStoreError("invalid ready evidence") from None
        if candidate != EpochCandidate(
            ready.provision_id, ready.body_id, ready.expected_last_epoch, ready.expected_generation, ready.next_epoch
        ):
            self._disabled = True
            raise EpochStoreError("ready evidence does not match candidate")
        connection: sqlite3.Connection | None = None
        try:
            connection = _connect_rw(self._directory / "epochs.sqlite3")
            _configure(connection)
            connection.execute("BEGIN IMMEDIATE")
            current = _read_row(connection, self._identity)
            if (
                (current.provision_id, current.body_id, current.last_epoch, current.generation)
                != (
                    candidate.provision_id,
                    candidate.body_id,
                    candidate.expected_last_epoch,
                    candidate.expected_generation,
                )
                or current.last_epoch >= _I63_MAX
                or current.generation >= _I63_MAX
            ):
                raise EpochStoreError("stale epoch candidate")
            new_generation = current.generation + 1
            record_hash = _row_hash(1, current.provision_id, current.body_id, candidate.next_epoch, new_generation)
            cursor = connection.execute(
                "UPDATE epoch_row SET last_epoch=?, generation=?, record_hash=? "
                "WHERE version=1 AND provision_id=? AND body_id=? AND last_epoch=? AND generation=? AND record_hash=?",
                (
                    candidate.next_epoch,
                    new_generation,
                    record_hash,
                    current.provision_id,
                    current.body_id,
                    current.last_epoch,
                    current.generation,
                    current.record_hash,
                ),
            )
            if cursor.rowcount != 1:
                raise EpochStoreError("epoch compare-and-set failed")
            connection.execute("COMMIT")
            connection.close()
            connection = None
            committed = self._read_row()
            if (committed.last_epoch, committed.generation) != (candidate.next_epoch, new_generation):
                raise EpochStoreError("epoch commit readback failed")
            return CommittedEpoch(committed.provision_id, committed.body_id, committed.last_epoch, committed.generation)
        except Exception as error:
            self._disabled = True
            if connection is not None:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                finally:
                    try:
                        connection.close()
                    except sqlite3.Error:
                        pass
            if isinstance(error, EpochStoreError):
                raise
            raise EpochStoreError("epoch commit outcome is unknown") from error

    def _read_row(self) -> "_Row":
        if self._custody_resources is not None:
            return self._custody_operation(self._custody_read)
        try:
            connection = _connect_rw(self._directory / "epochs.sqlite3")
            try:
                _configure(connection)
                return _read_row(connection, self._identity)
            finally:
                connection.close()
        except EpochStoreError:
            raise
        except sqlite3.Error as error:
            raise EpochStoreError("epoch database validation failed") from error

    def _ensure_enabled(self) -> None:
        if self._disabled or self._closed:
            raise EpochStoreError("epoch store is disabled")


@dataclass(frozen=True, slots=True)
class _Row:
    version: int
    provision_id: str
    body_id: str
    last_epoch: int
    generation: int
    record_hash: bytes

    def __iter__(self):
        return iter((self.version, self.provision_id, self.body_id, self.last_epoch, self.generation, self.record_hash))


def _directory(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_absolute() or path.is_symlink():
        raise EpochStoreError("invalid epoch directory")
    return path


def _identities(provision_id: str, body_id: str) -> tuple[str, str]:
    try:
        if (
            type(provision_id) is not str
            or type(body_id) is not str
            or str(UUID(provision_id)) != provision_id
            or str(UUID(body_id)) != body_id
            or UUID(provision_id).int == 0
            or UUID(body_id).int == 0
        ):
            raise ValueError
    except (TypeError, ValueError) as error:
        raise EpochStoreError("invalid provision identity") from error
    return provision_id, body_id


def _require_capability(platform: EpochPlatform, directory: Path) -> None:
    capability = platform.capability(directory)
    if not isinstance(capability, PlatformCapability) or not capability.verified:
        raise EpochStoreError("platform capability is not verified")


def _marker_identity(path: Path) -> tuple[str, str]:
    try:
        return _marker_bytes(path.read_bytes())
    except OSError as error:
        raise EpochStoreError("invalid provision marker") from error


def _marker_bytes(raw: bytes) -> tuple[str, str]:
    try:
        if type(raw) is not bytes or len(raw) > 4096:
            raise ValueError
        marker = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if (
            type(marker) is not dict
            or set(marker) != {"version", "provisionId", "bodyId"}
            or type(marker["version"]) is not int
            or marker["version"] != 1
            or type(marker["provisionId"]) is not str
            or type(marker["bodyId"]) is not str
        ):
            raise ValueError
        return _identities(marker["provisionId"], marker["bodyId"])
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, TypeError) as error:
        raise EpochStoreError("invalid provision marker") from error


def _connect_create(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path, isolation_level=None, timeout=0)


def _connect_rw(path: Path) -> sqlite3.Connection:
    uri = "file:" + quote(str(path).replace("\\", "/"), safe="/:") + "?mode=rw"
    return sqlite3.connect(uri, uri=True, isolation_level=None, timeout=0)


def _configure(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
        raise EpochStoreError("SQLite DELETE journal mode is unavailable")
    connection.execute("PRAGMA synchronous=EXTRA")
    if connection.execute("PRAGMA synchronous").fetchone() != (3,):
        raise EpochStoreError("SQLite EXTRA synchronous mode is unavailable")
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise EpochStoreError("SQLite integrity check failed")


def _read_row(connection: sqlite3.Connection, identity: tuple[str, str]) -> _Row:
    objects = connection.execute(
        "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    if objects != [("table", "epoch_row")]:
        raise EpochStoreError("unexpected epoch schema object")
    schema_sql = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='epoch_row'").fetchone()
    if schema_sql is None or type(schema_sql[0]) is not str or " ".join(schema_sql[0].split()) != _SCHEMA_NORMALIZED:
        raise EpochStoreError("invalid epoch schema")
    columns = connection.execute("PRAGMA table_info(epoch_row)").fetchall()
    if columns != [
        (0, "version", "INTEGER", 1, None, 0),
        (1, "provision_id", "TEXT", 1, None, 0),
        (2, "body_id", "TEXT", 1, None, 0),
        (3, "last_epoch", "INTEGER", 1, None, 0),
        (4, "generation", "INTEGER", 1, None, 0),
        (5, "record_hash", "BLOB", 1, None, 0),
    ]:
        raise EpochStoreError("invalid epoch schema")
    values = connection.execute(
        "SELECT version, provision_id, body_id, last_epoch, generation, record_hash, "
        "typeof(version), typeof(last_epoch), typeof(generation), typeof(record_hash) FROM epoch_row"
    ).fetchall()
    if len(values) != 1:
        raise EpochStoreError("invalid epoch row count")
    row = values[0]
    if row[6:] != ("integer", "integer", "integer", "blob"):
        raise EpochStoreError("invalid epoch field type")
    result = _Row(*row[:6])
    if (
        result.version != 1
        or (result.provision_id, result.body_id) != identity
        or not all(type(value) is int and 0 <= value <= _I63_MAX for value in (result.last_epoch, result.generation))
        or result.record_hash
        != _row_hash(result.version, result.provision_id, result.body_id, result.last_epoch, result.generation)
    ):
        raise EpochStoreError("invalid epoch row")
    return result


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    marker: dict[str, object] = {}
    for key, value in pairs:
        if key in marker:
            raise ValueError("duplicate marker key")
        marker[key] = value
    return marker


def _candidate_values(last_epoch: int, generation: int, next_epoch: int) -> None:
    if (
        type(last_epoch) is not int
        or type(generation) is not int
        or type(next_epoch) is not int
        or not 0 <= last_epoch < _I63_MAX
        or not 0 <= generation < _I63_MAX
        or next_epoch != last_epoch + 1
    ):
        raise ValueError("invalid epoch candidate")


def _validate_candidate(candidate: EpochCandidate) -> None:
    _identities(candidate.provision_id, candidate.body_id)
    _candidate_values(candidate.expected_last_epoch, candidate.expected_generation, candidate.next_epoch)


def _validate_ready(ready: VerifiedReadyContext) -> None:
    _validate_candidate(
        EpochCandidate(
            ready.provision_id,
            ready.body_id,
            ready.expected_last_epoch,
            ready.expected_generation,
            ready.next_epoch,
        )
    )
    if type(ready.evidence_id) is not str or not ready.evidence_id:
        raise ValueError("invalid ready evidence")


def _row_hash(version: int, provision_id: str, body_id: str, last_epoch: int, generation: int) -> bytes:
    fields = (
        _DOMAIN,
        str(version).encode("ascii"),
        provision_id.encode("ascii"),
        body_id.encode("ascii"),
        str(last_epoch).encode("ascii"),
        str(generation).encode("ascii"),
    )
    return hashlib.sha256(b"".join(struct.pack(">I", len(field)) + field for field in fields)).digest()
