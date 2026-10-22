"""Offline, single-body epoch counter backed by an independent SQLite database.

The default platform capability is deliberately disabled: SQLite transactional
semantics alone do not verify this host's ACL, VFS, lock, or power-loss claims.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
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
class PlatformCapability:
    """Externally verified platform evidence; production defaults to unsupported."""

    verified: bool
    reason: str


class LockHandle(Protocol):
    def close(self) -> None: ...


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
        self._operation_lock = threading.RLock()

    @classmethod
    def provision(
        cls, directory: str | Path, provision_id: str, body_id: str, *, platform: EpochPlatform | None = None
    ) -> "EpochStore":
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
            if lock is not None:
                try:
                    lock.close()
                except Exception:
                    pass
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
        except Exception:
            lock.close()
            raise
        return store

    def close(self) -> None:
        with self._operation_lock:
            if self._closed:
                return
            self._closed = True
            self._lock.close()

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
        raw = path.read_bytes()
        if len(raw) > 4096:
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
