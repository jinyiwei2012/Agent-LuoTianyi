"""Separately authorized native experiment; default skip precedes imports/setup/native calls.

Existing ancestry is inspected read-only and NEVER repaired. A skipped ancestry gate is
not native-positive evidence, and successful storage semantics do not prove durability.
"""

import ctypes
import os
from contextlib import contextmanager

import pytest


class ResourceRecord:
    """One fixture-only resource; failed output validity never licenses a guessed free."""

    def __init__(self, kind, releaser):
        self.kind = kind
        self.value = None
        self.output = ctypes.c_void_p() if kind == "LOCAL_ALLOC" else None
        self.releaser = releaser
        self.state = "ACQUIRING"
        self.close_attempted = False

    def close(self):
        if self.state == "RELEASED":
            return
        if self.state != "OWNED" or self.close_attempted:
            self.state = "UNKNOWN"
            raise RuntimeError("fixture cleanup outcome is unknown")
        self.close_attempted = True
        self.state = "CLOSING"
        try:
            self.releaser(self.value)
        except BaseException:
            self.state = "UNKNOWN"
            raise
        if self.state != "CLOSING":
            self.state = "UNKNOWN"
            raise RuntimeError("fixture cleanup outcome is unknown")
        self.value = None
        if self.output is not None:
            self.output.value = None
        self.releaser = None
        self.state = "RELEASED"


class NativeFixtureOwner:
    """Bounded session-owned quarantine, independent of test tracebacks; never retries cleanup."""

    def __init__(self):
        self._slots = [None] * 8
        self.directories = [None] * 2
        self.unknown = False
        # At most store/open receipt + descriptor receipt + SQL + journal handle + conversion,
        # with room for all independent failure records; no growth after UNKNOWN.
        self._records = [None] * 8

    def reserve(self, kind, releaser):
        if self.unknown:
            raise RuntimeError("fixture owner is quarantined")
        for index, record in enumerate(self._records):
            if record is None or record.state == "RELEASED":
                record = ResourceRecord(kind, releaser)
                self._records[index] = record
                return record
        raise RuntimeError("fixture resource capacity exhausted")

    def acquire(self, kind, factory, releaser):
        record = self.reserve(kind, releaser)
        try:
            record.value = factory()
        except BaseException:
            if record.state == "ACQUIRING":
                record.state = "RELEASED"  # No returned object; output APIs use convert instead.
            else:
                self.mark_unknown()
            raise
        if record.state != "ACQUIRING":
            record.state = "UNKNOWN"
            self.mark_unknown()
            raise RuntimeError("fixture acquisition invalidated")
        record.state = "OWNED"
        return record

    def convert(self, query, releaser):
        record = self.reserve("LOCAL_ALLOC", releaser)
        try:
            success = query(record.output)
        except BaseException:
            record.state = "UNKNOWN" if record.output.value or record.state != "ACQUIRING" else "RELEASED"
            self.unknown |= record.state == "UNKNOWN"
            raise
        if not success or not record.output.value or record.state != "ACQUIRING":
            record.state = "UNKNOWN" if record.output.value or record.state != "ACQUIRING" else "RELEASED"
            self.unknown |= record.state == "UNKNOWN"
            raise RuntimeError("fixture conversion failed or cleanup outcome is unknown")
        record.value = record.output.value
        record.state = "OWNED"
        return record

    def close_records(self, *records):
        first = None
        for record in records:
            try:
                record.close()
            except BaseException as failure:
                self.mark_unknown()
                if first is None or isinstance(first, Exception) and not isinstance(failure, Exception):
                    first = failure
        if first is not None:
            raise first

    @contextmanager
    def resource(self, kind, factory, releaser):
        record = self.acquire(kind, factory, releaser)
        with self.using(record) as value:
            yield value

    @contextmanager
    def using(self, record):
        first = None
        try:
            yield record.value
        except BaseException as failure:
            first = failure
        try:
            self.close_records(record)
        except BaseException as failure:
            if first is None or isinstance(first, Exception) and not isinstance(failure, Exception):
                first = failure
        if first is not None:
            raise first

    def cleanup_directory(self, name, remover):
        if self.unknown or any(r is not None and r.state != "RELEASED" for r in self._records):
            self.mark_unknown()
            raise RuntimeError("fixture directory retained: cleanup outcome is unknown")
        remover(name)

    def retain(self, resource):
        for index, value in enumerate(self._slots):
            if value is None:
                self._slots[index] = resource
                return resource
        raise RuntimeError("native fixture owner capacity exhausted")

    def released(self, resource):
        for index, value in enumerate(self._slots):
            if value is resource:
                self._slots[index] = None
                return
        raise RuntimeError("native fixture owner identity mismatch")

    def retain_directory(self, name):
        for index, value in enumerate(self.directories):
            if value is None:
                self.directories[index] = name
                return
        raise RuntimeError("native fixture directory capacity exhausted")

    def mark_unknown(self):
        self.unknown = True

    def assess_ancestry(self, assessment):
        from src.adapter.minecraft.epoch_store import EpochStoreError

        try:
            assessment()
        except EpochStoreError:
            if self.unknown:
                raise
            pytest.skip("UNSUPPORTED_EXISTING_ANCESTRY: restricted owner/explicit-ALLOW/fixed-NTFS policy")

    @contextmanager
    def store_lifetime(self, store):
        """Store is already retained; close once without masking the body's first fatal."""
        first = None
        try:
            yield store
        except BaseException as failure:
            first = failure
        try:
            store.close()
        except BaseException as failure:
            self.mark_unknown()
            if first is None or isinstance(first, Exception) and not isinstance(failure, Exception):
                first = failure
        else:
            self.released(store)
        if first is not None:
            raise first


@pytest.fixture(scope="session")
def native_fixture_owner():
    if os.environ.get("RUN_EPOCH_CUSTODY_NATIVE") != "1" or os.name != "nt":
        pytest.skip("native custody opt-in/Windows prerequisites disabled")
    # No finalizer: process/session end is NOT confirmed resource release.
    return NativeFixtureOwner()


@pytest.mark.skipif(os.environ.get("RUN_EPOCH_CUSTODY_NATIVE") != "1", reason="native custody opt-in disabled")
@pytest.mark.skipif(os.name != "nt", reason="Windows NTFS custody required")
def test_native_epoch_custody_restricted_fixture(  # noqa: C901 - explicit native cleanup boundaries
    native_fixture_owner,
):
    import ctypes
    import json
    import shutil
    import sqlite3
    import subprocess
    import sys
    import tempfile
    from contextlib import contextmanager
    from pathlib import Path, PureWindowsPath

    from src.adapter.minecraft.deployment_acl import DescriptorAllocationReceipt, inspect_handle_security_descriptor
    from src.adapter.minecraft.epoch_platform import (
        NativeEpochOpenProfile,
        WindowsEpochCustodyPlatform,
        _assess_epoch,
        _EpochNativeApi,
        _journal_prediction,
        _match_journal,
        _NativeEpochLease,
    )
    from src.adapter.minecraft.epoch_store import (
        _SCHEMA,
        EpochOpenAttemptReceipt,
        EpochStore,
        TrustedEpochPolicy,
        VerifiedReadyContext,
        _row_hash,
    )

    approved = Path(r"C:\Users\Administrator\AppData\Local\Temp\opencode")
    assert approved.is_dir() and not approved.is_symlink()
    containing_owner = native_fixture_owner
    temporary_owner = containing_owner.retain(_NativeEpochLease(None, None, []))
    api = _EpochNativeApi(temporary_owner)
    try:
        owner = api.token_owner()
        temporary_owner.close()
    except BaseException:
        containing_owner.mark_unknown()
        raise
    containing_owner.released(temporary_owner)

    @contextmanager
    def owned_directory():
        name = tempfile.mkdtemp(prefix="epoch-native-fixture-", dir=approved)
        containing_owner.retain_directory(name)
        clean = False
        try:
            yield name
            clean = True
        finally:
            if clean and not containing_owner.unknown:
                containing_owner.cleanup_directory(name, shutil.rmtree)
            else:
                containing_owner.mark_unknown()
                print("NATIVE_FIXTURE_RETAINED_UNKNOWN " + name)

    def inspect_owned(handle):
        receipt = DescriptorAllocationReceipt()
        containing_owner.retain(receipt)
        try:
            result = inspect_handle_security_descriptor(handle, allocation=receipt)
        except BaseException:
            if receipt.snapshot().state != "RELEASED":
                containing_owner.mark_unknown()
            else:
                containing_owner.released(receipt)
            raise
        assert receipt.snapshot().state == "RELEASED"
        containing_owner.released(receipt)
        return result

    profile = NativeEpochOpenProfile(owner, "EXPLICIT_INHERITABLE_ALLOW_V1")
    provision = "10000000-0000-4000-8000-000000000001"
    body = "20000000-0000-4000-8000-000000000002"
    # Test policy trusts ONLY the effective token owner. No inferred Administrator/SYSTEM trust.
    trial = TrustedEpochPolicy(str(approved), 1, 1, owner, (owner,), (owner,), (owner,), provision, body)
    ancestry = list(reversed(approved.parents)) + [approved]
    for path in ancestry:

        def assess_path():
            api.mapping(PureWindowsPath(str(path)).anchor)
            with containing_owner.resource("HANDLE", lambda: api.open(str(path), "directory"), api.close) as handle:
                _assess_epoch(inspect_owned(handle), trial, True, False)

        containing_owner.assess_ancestry(assess_path)

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    advapi.ConvertSidToStringSidW.restype = ctypes.c_int
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = ctypes.c_int
    advapi.SetFileSecurityW.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_void_p)
    advapi.SetFileSecurityW.restype = ctypes.c_int
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel.LocalFree.restype = ctypes.c_void_p
    sid_buffer = ctypes.create_string_buffer(owner)

    def local_free(pointer):
        if kernel.LocalFree(pointer):
            raise RuntimeError("fixture LocalFree cleanup outcome is unknown")

    text = containing_owner.convert(
        lambda output: advapi.ConvertSidToStringSidW(sid_buffer, ctypes.byref(output)), local_free
    )
    with containing_owner.using(text) as pointer:
        owner_text = ctypes.wstring_at(pointer)

    def protect_created(path, directory):
        # Only fresh objects created by this fixture. Remove unsupported file 0x40 explicitly.
        flags = "OICI" if directory else ""
        sddl = f"O:{owner_text}D:P(A;{flags};0x001f01bf;;;{owner_text})"
        descriptor = containing_owner.convert(
            lambda output: advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, ctypes.byref(output), None
            ),
            local_free,
        )
        with containing_owner.using(descriptor) as pointer:
            assert advapi.SetFileSecurityW(str(path), 0x80000005, pointer)

    with owned_directory() as name:
        root = Path(name)
        protect_created(root, True)
        (root / "epochs.lock").touch()
        (root / "provision.json").write_text(
            json.dumps(dict(version=1, provisionId=provision, bodyId=body)), encoding="utf-8"
        )
        database = root / "epochs.sqlite3"
        with containing_owner.resource(
            "SQLITE", lambda: sqlite3.connect(database, isolation_level=None), lambda connection: connection.close()
        ) as connection:
            connection.execute(_SCHEMA)
            connection.execute(
                "INSERT INTO epoch_row VALUES(?,?,?,?,?,?)",
                (1, provision, body, 0, 0, _row_hash(1, provision, body, 0, 0)),
            )
        for child in root.iterdir():
            protect_created(child, False)
        with containing_owner.resource("HANDLE", lambda: api.open(str(root), "directory"), api.close) as handle:
            identity = api.info(handle)
            root_descriptor = inspect_owned(handle)
        policy = TrustedEpochPolicy(
            str(root), identity["volume"], identity["index"], owner, (owner,), (owner,), (owner,), provision, body
        )
        backend = WindowsEpochCustodyPlatform(profile)
        opening = containing_owner.retain(EpochOpenAttemptReceipt())
        try:
            store = EpochStore.open_with_custody(policy, custody=backend, receipt=opening)
        except BaseException:
            if opening.snapshot().state == "UNKNOWN":
                containing_owner.mark_unknown()
            else:
                containing_owner.released(opening)
            raise
        containing_owner.retain(store)
        containing_owner.released(opening)
        with containing_owner.store_lifetime(store):
            candidate = store.read_candidate()
            # Same-process SQLite rw compatibility while metadata pin is held; real journal exists in transaction.
            with containing_owner.resource(
                "SQLITE",
                lambda: sqlite3.connect(database, isolation_level=None, timeout=0),
                lambda connection: connection.close(),
            ) as sql:
                sql.execute("PRAGMA journal_mode=DELETE")
                sql.execute("BEGIN IMMEDIATE")
                sql.execute("UPDATE epoch_row SET generation=generation")
                journal = root / "epochs.sqlite3-journal"
                assert journal.exists()
                with containing_owner.resource(
                    "HANDLE", lambda: api.open(str(journal), "db"), api.close
                ) as journal_handle:
                    actual = inspect_owned(journal_handle)
                    assert actual.owner_sid == owner
                    _match_journal(actual, _journal_prediction(root_descriptor, policy, profile, owner), policy)
                sql.execute("ROLLBACK")
                assert not journal.exists()
            probe = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import ctypes,sys; from ctypes import wintypes as w; "
                    "k=ctypes.WinDLL('kernel32',use_last_error=True); "
                    "k.CreateFileW.argtypes=(w.LPCWSTR,w.DWORD,w.DWORD,w.LPVOID,w.DWORD,w.DWORD,w.HANDLE); "
                    "k.CreateFileW.restype=w.HANDLE; k.CloseHandle.argtypes=(w.HANDLE,); "
                    "h=k.CreateFileW(sys.argv[1],0xC0020080,0,None,3,0x200000,None); "
                    "e=ctypes.get_last_error(); "
                    "\nif h==ctypes.c_void_p(-1).value: sys.exit(0 if e==32 else 8)"
                    "\nelse: k.CloseHandle(h); sys.exit(9)",
                    str(root / "epochs.lock"),
                ],
                timeout=5,
                check=False,
            )
            assert probe.returncode == 0
            with pytest.raises(OSError):
                root.rename(root.with_name(root.name + "-renamed"))
            committed = store.commit_ready(candidate, VerifiedReadyContext(provision, body, 0, 0, 1, "native-test"))
            assert committed.last_epoch == 1
        opening = containing_owner.retain(EpochOpenAttemptReceipt())
        try:
            reopened = EpochStore.open_with_custody(policy, custody=backend, receipt=opening)
            containing_owner.retain(reopened)
            containing_owner.released(opening)
            reopened.close()
        except BaseException:
            containing_owner.mark_unknown()
            raise
        else:
            containing_owner.released(reopened)
