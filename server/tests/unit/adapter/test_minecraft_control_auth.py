from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event, Thread

import pytest

import src.adapter.minecraft.control_auth as control_auth
from src.adapter.minecraft.control_auth import (
    AuthenticatedHandshake,
    ControlAuthContext,
    ControlAuthError,
    PendingHandshakes,
    b32_decode,
    b32_encode,
    compute_proof,
    transcript,
    verify_proof,
)

NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)
EXPIRES = NOW + timedelta(seconds=5)
CTX = ControlAuthContext(
    peer_role="CLIENT_CONTROLLER",
    peer_install_id="client-1",
    hub_install_id="hub-1",
    key_id="test-key-1",
    protocol="luosandbox.minecraft.v1",
    mod_hash=b"\0" * 32,
    config_hash=b"\x33" * 32,
    binding_hash=b"\x44" * 32,
)
KEY = bytes(range(32))
PEER = b"\x11" * 32
HUB = b"\x22" * 32
CID = "00000000-0000-4000-8000-000000000001"
CONN = "00000000-0000-4000-8000-000000000010"
SECOND = "00000000-0000-4000-8000-000000000011"


def proof(label: str, context: ControlAuthContext = CTX, key: bytes = KEY) -> str:
    return compute_proof(key, label, context, PEER, HUB, CID, NOW, EXPIRES)


def test_fixed_peer_proof_vector_and_exact_transcript():
    value = transcript("PEER_PROOF", CTX, PEER, HUB, CID, NOW, EXPIRES)
    assert len(value) == 416
    assert value.hex().startswith(
        "0000001a6c756f73616e64626f782e636f6e74726f6c2d617574682e76310000000a504545525f50524f4f46"
    )
    assert b32_decode(proof("PEER_PROOF")).hex() == "95141dc1c381eb134b3d1f3df405d6e2ee2058e31f99b1269348b4f56f2a4921"


def test_all_labels_are_direction_separated_and_reject_reflection_tamper_and_wrong_key():
    values = {label: proof(label) for label in ("HUB_PROOF", "PEER_PROOF", "READY_PROOF")}
    assert len(set(values.values())) == 3
    for label, value in values.items():
        assert verify_proof(value, KEY, label, CTX, PEER, HUB, CID, NOW, EXPIRES)
    assert not verify_proof(values["HUB_PROOF"], KEY, "PEER_PROOF", CTX, PEER, HUB, CID, NOW, EXPIRES)
    assert not verify_proof(values["PEER_PROOF"], b"x" * 32, "PEER_PROOF", CTX, PEER, HUB, CID, NOW, EXPIRES)
    assert not verify_proof(values["PEER_PROOF"], KEY, "PEER_PROOF", CTX, b"x" * 32, HUB, CID, NOW, EXPIRES)


def test_b32_is_canonical_and_strictly_32_bytes():
    value = b32_encode(KEY)
    assert len(value) == 43 and b32_decode(value) == KEY
    for malformed in (value + "=", value[:-1], value[:-1] + "+", "B" * 43):
        with pytest.raises(ControlAuthError):
            b32_decode(malformed)
    with pytest.raises(ControlAuthError):
        b32_encode(b"x" * 31)


def test_utc_and_monotonic_inputs_fail_closed():
    naive = NOW.replace(tzinfo=None)
    unaligned = NOW.replace(microsecond=1)
    for value in (naive, unaligned):
        with pytest.raises(ControlAuthError):
            transcript("PEER_PROOF", CTX, PEER, HUB, CID, value, EXPIRES)
    pending = PendingHandshakes()
    for clock in (True, -1, float("inf")):
        with pytest.raises(ControlAuthError):
            pending.begin("conn", CTX, PEER, NOW, now=clock, utc_now=NOW)
    with pytest.raises(ControlAuthError):
        PendingHandshakes(ttl_seconds=4)


def test_success_consumes_state_and_returns_immutable_ready_context():
    pending = PendingHandshakes()
    item = pending.begin(CID, CTX, PEER, NOW, hub_nonce=HUB, challenge_id=CID, now=0, utc_now=NOW)
    auth = pending.consume(CID, CID, proof("PEER_PROOF"), KEY, expected_context=CTX, now=1, utc_now=NOW)
    assert isinstance(auth, AuthenticatedHandshake)
    assert auth.context is CTX
    assert auth.connection_id == CID and auth.peer_nonce == PEER and auth.hub_nonce == HUB
    assert auth.issued_at == NOW and auth.expires_at == EXPIRES and auth.has_full_provenance
    assert verify_proof(auth.ready_proof, KEY, "READY_PROOF", CTX, PEER, HUB, CID, NOW, item.expires_at)
    with pytest.raises((AttributeError, TypeError)):
        auth.challenge_id = "changed"  # type: ignore[misc]
    with pytest.raises(ControlAuthError):
        pending.consume(CID, CID, proof("PEER_PROOF"), KEY, now=1, utc_now=NOW)


def test_invalid_proof_and_context_mismatch_consume_pending_state():
    other = ControlAuthContext(
        peer_role="CLIENT_CONTROLLER",
        peer_install_id="other-client",
        hub_install_id="hub-1",
        key_id="test-key-1",
        protocol="luosandbox.minecraft.v1",
        mod_hash=b"\0" * 32,
        config_hash=b"\x33" * 32,
        binding_hash=b"\x44" * 32,
    )
    pending = PendingHandshakes()
    pending.begin("conn", CTX, PEER, NOW, hub_nonce=HUB, challenge_id=CID, now=0, utc_now=NOW)
    with pytest.raises(ControlAuthError):
        pending.consume("conn", CID, "malformed", KEY, now=1, utc_now=NOW)
    with pytest.raises(ControlAuthError):
        pending.consume("conn", CID, proof("PEER_PROOF"), KEY, now=1, utc_now=NOW)
    pending.begin("conn", CTX, PEER, NOW, hub_nonce=HUB, challenge_id=CID, now=2, utc_now=NOW)
    with pytest.raises(ControlAuthError):
        pending.consume("conn", CID, proof("PEER_PROOF"), KEY, expected_context=other, now=3, utc_now=NOW)


def test_expired_slots_recover_and_cross_connection_proof_is_rejected():
    pending = PendingHandshakes(maximum=1)
    pending.begin(CONN, CTX, PEER, NOW, hub_nonce=HUB, challenge_id=CID, now=0, utc_now=NOW)
    pending.begin(SECOND, CTX, PEER, NOW, hub_nonce=HUB, challenge_id=CID, now=5, utc_now=NOW)
    with pytest.raises(ControlAuthError):
        pending.consume(CONN, CID, proof("PEER_PROOF"), KEY, now=5, utc_now=NOW)
    with pytest.raises(ControlAuthError):
        pending.consume("second-other", CID, proof("PEER_PROOF"), KEY, now=5, utc_now=NOW)
    assert pending.consume(SECOND, CID, proof("PEER_PROOF"), KEY, now=5, utc_now=NOW).challenge_id == CID


def test_wall_clock_future_and_expiry_are_rejected_when_caller_supplies_it():
    pending = PendingHandshakes()
    with pytest.raises(ControlAuthError):
        pending.begin("conn", CTX, PEER, NOW, now=0, utc_now=NOW - timedelta(seconds=6))
    pending.begin("conn", CTX, PEER, NOW, hub_nonce=HUB, challenge_id=CID, now=0, utc_now=NOW)
    with pytest.raises(ControlAuthError):
        pending.consume("conn", CID, proof("PEER_PROOF"), KEY, now=1, utc_now=EXPIRES)


def test_trusted_utc_is_required_and_monotonic_rollback_consumes_challenge():
    pending = PendingHandshakes()
    with pytest.raises(TypeError):
        pending.begin("conn", CTX, PEER, NOW, now=0)
    pending.begin("conn", CTX, PEER, NOW, hub_nonce=HUB, challenge_id=CID, now=100, utc_now=NOW)
    with pytest.raises(TypeError):
        pending.consume("conn", CID, proof("PEER_PROOF"), KEY, now=101)
    with pytest.raises(ControlAuthError):
        pending.consume("conn", CID, proof("PEER_PROOF"), KEY, now=1, utc_now=NOW)
    with pytest.raises(ControlAuthError):
        pending.consume("conn", CID, proof("PEER_PROOF"), KEY, now=101, utc_now=NOW)


def test_secret_bearing_dataclasses_do_not_expose_values_in_repr():
    pending = PendingHandshakes()
    item = pending.begin(CONN, CTX, PEER, NOW, hub_nonce=HUB, challenge_id=CID, now=0, utc_now=NOW)
    auth = pending.consume(CONN, CID, proof("PEER_PROOF"), KEY, now=1, utc_now=NOW)
    for value in (CTX, item, auth):
        rendered = repr(value)
        assert PEER.hex() not in rendered
        assert HUB.hex() not in rendered
        assert auth.ready_proof not in rendered


def test_concurrent_begin_never_exceeds_four_and_same_connection_has_one_winner():
    pending = PendingHandshakes()
    barrier = Barrier(8)

    def begin(index: int) -> bool:
        barrier.wait()
        try:
            pending.begin(f"conn-{index}", CTX, PEER, NOW, now=0, utc_now=NOW)
            return True
        except ControlAuthError:
            return False

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(begin, range(8)))
    assert sum(outcomes) == 4

    one = PendingHandshakes()
    barrier = Barrier(8)

    def same_connection() -> bool:
        barrier.wait()
        try:
            one.begin("same", CTX, PEER, NOW, now=0, utc_now=NOW)
            return True
        except ControlAuthError:
            return False

    with ThreadPoolExecutor(max_workers=8) as executor:
        assert sum(executor.map(lambda _: same_connection(), range(8))) == 1


def test_consume_holds_lock_through_proof_and_ready_construction(monkeypatch):
    pending = PendingHandshakes()
    pending.begin(CONN, CTX, PEER, NOW, hub_nonce=HUB, challenge_id=CID, now=0, utc_now=NOW)
    entered = Event()
    release = Event()
    original_verify = control_auth.verify_proof
    result: list[object] = []

    def blocking_verify(*args: object, **kwargs: object) -> bool:
        entered.set()
        assert release.wait(timeout=2)
        return original_verify(*args, **kwargs)

    monkeypatch.setattr(control_auth, "verify_proof", blocking_verify)

    def consume() -> None:
        result.append(pending.consume(CONN, CID, proof("PEER_PROOF"), KEY, now=1, utc_now=NOW))

    consumer = Thread(target=consume)
    consumer.start()
    assert entered.wait(timeout=1)
    begin_finished = Event()

    def begin_replacement() -> None:
        pending.begin(CONN, CTX, PEER, NOW, now=2, utc_now=NOW)
        begin_finished.set()

    replacement = Thread(target=begin_replacement)
    replacement.start()
    assert not begin_finished.wait(timeout=0.1)
    release.set()
    consumer.join(timeout=2)
    replacement.join(timeout=2)
    assert not consumer.is_alive() and not replacement.is_alive()
    assert len(result) == 1 and begin_finished.is_set()


def test_discard_removes_only_owned_connection_and_is_idempotent():
    pending = PendingHandshakes()
    pending.begin("one", CTX, PEER, NOW, now=0, utc_now=NOW)
    pending.begin("two", CTX, PEER, NOW, now=0, utc_now=NOW)
    assert pending.discard("one") is True
    assert pending.discard("one") is False
    with pytest.raises(ControlAuthError):
        pending.consume("one", CID, proof("PEER_PROOF"), KEY, now=1, utc_now=NOW)
    assert pending.discard("two") is True
