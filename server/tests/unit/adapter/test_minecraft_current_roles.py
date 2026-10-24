import asyncio
import copy
from dataclasses import replace
from datetime import datetime, timezone
from threading import Thread

import pytest

from src.adapter.minecraft.control_auth import (
    AuthenticatedHandshake,
    ControlAuthContext,
    PendingHandshakes,
    compute_proof,
)
from src.adapter.minecraft.current_roles import (
    AuthenticatedControlRoleOwner,
    AuthenticatedRoleLease,
    CurrentRoleError,
    RoleWithdrawal,
)

NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)
CONNECTION = "10000000-0000-4000-8000-000000000001"
CHALLENGE = "20000000-0000-4000-8000-000000000001"


def context(role="SERVER_AUTHORITY", install="server"):
    return ControlAuthContext(
        peer_role=role,
        peer_install_id=install,
        hub_install_id="hub",
        key_id="key",
        protocol="luosandbox.minecraft.v1",
        mod_hash=b"m" * 32,
        config_hash=b"c" * 32,
        binding_hash=b"b" * 32,
    )


def handshake(value, connection=CONNECTION, challenge=CHALLENGE):
    key = b"k" * 32
    pending = PendingHandshakes()
    item = pending.begin(
        connection,
        value,
        b"p" * 32,
        NOW,
        hub_nonce=b"h" * 32,
        challenge_id=challenge,
        now=0,
        utc_now=NOW,
    )
    proof = compute_proof(
        key,
        "PEER_PROOF",
        value,
        item.peer_nonce,
        item.hub_nonce,
        item.challenge_id,
        item.issued_at,
        item.expires_at,
    )
    return pending.consume(
        connection,
        challenge,
        proof,
        key,
        expected_context=value,
        now=1,
        utc_now=NOW,
    )


@pytest.mark.asyncio
async def test_exact_two_roles_duplicate_rejection_and_single_use_promotion():
    server, client = context(), context("CLIENT_CONTROLLER", "client")
    owner = AuthenticatedControlRoleOwner((server, client), "hub")
    reservation = owner.reserve(server, CONNECTION)
    with pytest.raises(CurrentRoleError):
        owner.reserve(server, "10000000-0000-4000-8000-000000000002")
    owner.bind_challenge(reservation, CHALLENGE)
    lease = owner.promote(reservation, handshake(server))
    assert owner.current("SERVER_AUTHORITY") is lease and owner.is_current(lease)
    assert owner.active_roles() == ("SERVER_AUTHORITY",)
    with pytest.raises(CurrentRoleError):
        owner.promote(reservation, handshake(server))
    withdrawal = owner.withdraw(lease)
    assert withdrawal.role == "SERVER_AUTHORITY" and withdrawal.connection_id == CONNECTION
    assert owner.withdraw(lease) is None and owner.active_roles() == ()


@pytest.mark.asyncio
async def test_currentness_is_exact_identity_and_withdraw_callback_observes_removed_slot():
    server = context()
    owner = AuthenticatedControlRoleOwner((server,), "hub")
    reservation = owner.reserve(server, CONNECTION)
    owner.bind_challenge(reservation, CHALLENGE)
    lease = owner.promote(reservation, handshake(server))
    copied = copy.copy(lease)
    assert not owner.is_current(copied)
    observed = []

    def after_remove(withdrawal):
        observed.append((withdrawal, owner.current("SERVER_AUTHORITY")))

    withdrawal = owner.withdraw(lease, after_remove=after_remove)
    assert observed == [(withdrawal, None)]
    assert not owner.is_current(lease)


@pytest.mark.asyncio
async def test_withdraw_callback_exception_never_restores_authority():
    server = context()
    owner = AuthenticatedControlRoleOwner((server,), "hub")
    reservation = owner.reserve(server, CONNECTION)
    owner.bind_challenge(reservation, CHALLENGE)
    lease = owner.promote(reservation, handshake(server))

    def fail(_withdrawal):
        assert owner.current("SERVER_AUTHORITY") is None
        raise RuntimeError("callback failed")

    with pytest.raises(RuntimeError, match="callback failed"):
        owner.withdraw(lease, after_remove=fail)
    assert owner.current("SERVER_AUTHORITY") is None and not owner.is_current(lease)


@pytest.mark.asyncio
async def test_reservation_withdrawal_and_copied_receipt_lack_authenticated_provenance():
    server = context()
    owner = AuthenticatedControlRoleOwner((server,), "hub")
    reservation = owner.reserve(server, CONNECTION)
    receipt = owner.withdraw(reservation)
    assert receipt is None and owner._withdrawals == {}
    lease_like = AuthenticatedRoleLease(
        "SERVER_AUTHORITY",
        CONNECTION,
        1,
        handshake(server),
        object(),
    )
    forged = RoleWithdrawal(lease_like.role, lease_like.connection_id, lease_like.generation)
    assert not owner.consume_withdrawal(forged, lease_like)


@pytest.mark.asyncio
async def test_owner_held_withdrawal_registry_is_bounded_and_one_use():
    server, client = context(), context("CLIENT_CONTROLLER", "client")
    owner = AuthenticatedControlRoleOwner((server, client), "hub")
    for index, value in enumerate((server, client)):
        connection = f"10000000-0000-4000-8000-{index + 1:012x}"
        challenge = f"20000000-0000-4000-8000-{index + 1:012x}"
        reservation = owner.reserve(value, connection)
        owner.bind_challenge(reservation, challenge)
        lease = owner.promote(reservation, handshake(value, connection=connection, challenge=challenge))
        forged = RoleWithdrawal(lease.role, lease.connection_id, lease.generation)
        assert owner._withdrawals == {} and not owner.consume_withdrawal(forged, lease)
        receipt = owner.withdraw(lease)
        assert len(owner._withdrawals) <= 2
        copied = copy.copy(receipt)
        assert not owner.consume_withdrawal(copied, lease)
        assert owner.consume_withdrawal(receipt, lease)
        assert not owner.consume_withdrawal(receipt, lease)


def test_context_compatibility_does_not_bind_owner_loop():
    server = context()
    owner = AuthenticatedControlRoleOwner((server,), "hub")
    assert owner.matches_contexts((server,), "hub")
    assert not owner.matches_contexts((replace(server),), "hub")
    assert owner._loop is None and owner._thread is None


@pytest.mark.asyncio
async def test_promotion_requires_exact_reservation_challenge_connection_context_and_full_provenance():
    server = context()
    variants = (
        handshake(server, connection="10000000-0000-4000-8000-000000000002"),
        handshake(server, challenge="20000000-0000-4000-8000-000000000002"),
        handshake(context(install="other")),
        AuthenticatedHandshake(context=server, challenge_id=CHALLENGE, ready_proof="legacy"),
    )
    for candidate in variants:
        owner = AuthenticatedControlRoleOwner((server,), "hub")
        reservation = owner.reserve(server, CONNECTION)
        owner.bind_challenge(reservation, CHALLENGE)
        with pytest.raises(CurrentRoleError):
            owner.promote(reservation, candidate)


@pytest.mark.asyncio
async def test_only_actual_successful_consume_can_promote_and_wrong_proof_cannot_activate():
    server = context()
    owner = AuthenticatedControlRoleOwner((server,), "hub")
    reservation = owner.reserve(server, CONNECTION)
    owner.bind_challenge(reservation, CHALLENGE)
    pending = PendingHandshakes()
    pending.begin(
        CONNECTION,
        server,
        b"p" * 32,
        NOW,
        hub_nonce=b"h" * 32,
        challenge_id=CHALLENGE,
        now=0,
        utc_now=NOW,
    )
    with pytest.raises(Exception):
        pending.consume(CONNECTION, CHALLENGE, "bad", b"k" * 32, now=1, utc_now=NOW)
    assert owner.active_roles() == ()
    with pytest.raises(Exception):
        pending.consume(CONNECTION, CHALLENGE, "bad", b"k" * 32, now=1, utc_now=NOW)
    assert owner.active_roles() == ()


@pytest.mark.asyncio
async def test_public_construction_replace_and_copy_never_preserve_consumed_provenance():
    server = context()
    real = handshake(server)
    assert real.has_full_provenance
    legacy = AuthenticatedHandshake(context=server, challenge_id=CHALLENGE, ready_proof=real.ready_proof)
    assert not legacy.has_full_provenance
    with pytest.raises(TypeError):
        AuthenticatedHandshake(
            context=server,
            challenge_id=CHALLENGE,
            ready_proof=real.ready_proof,
            connection_id=CONNECTION,
        )
    reconstructed = AuthenticatedHandshake(
        context=real.context, challenge_id=real.challenge_id, ready_proof=real.ready_proof
    )
    for untrusted in (
        legacy,
        reconstructed,
        replace(real),
        replace(real, challenge_id="20000000-0000-4000-8000-000000000002"),
        copy.copy(real),
        copy.deepcopy(real),
    ):
        assert not untrusted.has_full_provenance
        owner = AuthenticatedControlRoleOwner((server,), "hub")
        reservation = owner.reserve(server, CONNECTION)
        owner.bind_challenge(reservation, CHALLENGE)
        with pytest.raises(CurrentRoleError):
            owner.promote(reservation, untrusted)


@pytest.mark.asyncio
async def test_equal_string_stale_token_cannot_remove_new_generation_and_repr_is_redacted():
    server = context()
    owner = AuthenticatedControlRoleOwner((server,), "hub")
    old = owner.reserve(server, CONNECTION)
    owner.bind_challenge(old, CHALLENGE)
    owner.withdraw(old)
    new = owner.reserve(server, CONNECTION)
    assert owner.withdraw(old) is None
    assert "mod_hash" not in repr(new) and "ready" not in repr(new)
    owner.bind_challenge(new, CHALLENGE)
    lease = owner.promote(new, handshake(server))
    assert owner.active_roles() == ("SERVER_AUTHORITY",)
    assert owner.withdraw(old) is None
    assert owner.active_roles() == ("SERVER_AUTHORITY",)
    assert owner.withdraw(lease) is not None


@pytest.mark.asyncio
async def test_owner_loop_and_thread_gate_precedes_mutation():
    server = context()
    owner = AuthenticatedControlRoleOwner((server,), "hub")
    reservation = owner.reserve(server, CONNECTION)
    errors = []

    def wrong_thread():
        try:
            asyncio.run(_withdraw(owner, reservation))
        except Exception as error:
            errors.append(error)

    thread = Thread(target=wrong_thread)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive() and isinstance(errors[0], CurrentRoleError)
    owner.bind_challenge(reservation, CHALLENGE)
    assert owner.promote(reservation, handshake(server))


async def _withdraw(owner, token):
    owner.withdraw(token)


@pytest.mark.asyncio
async def test_generation_exhaustion_closes_without_wrap(monkeypatch):
    server, client = context(), context("CLIENT_CONTROLLER", "client")
    owner = AuthenticatedControlRoleOwner((server, client), "hub")
    reservation = owner.reserve(server, CONNECTION)
    owner.bind_challenge(reservation, CHALLENGE)
    lease = owner.promote(reservation, handshake(server))
    monkeypatch.setattr(owner, "_generation", (1 << 63) - 1)
    with pytest.raises(CurrentRoleError, match="exhausted"):
        owner.reserve(client, "10000000-0000-4000-8000-000000000002")
    with pytest.raises(CurrentRoleError):
        owner.reserve(client, "10000000-0000-4000-8000-000000000002")
    assert owner.current("SERVER_AUTHORITY") is lease
    observed = []
    owner.withdraw(lease, after_remove=observed.append)
    assert len(observed) == 1 and owner.current("SERVER_AUTHORITY") is None


def test_constructor_rejects_payload_style_clone_context_and_wrong_hub():
    server = context()
    with pytest.raises(ValueError):
        AuthenticatedControlRoleOwner((server, replace(server)), "hub")
    with pytest.raises(ValueError):
        AuthenticatedControlRoleOwner((server,), "other")


def test_different_loop_on_same_thread_is_rejected_without_mutation():
    server = context()
    owner = AuthenticatedControlRoleOwner((server,), "hub")

    async def bind():
        return owner.reserve(server, CONNECTION)

    reservation = asyncio.run(bind())

    async def wrong_loop():
        with pytest.raises(CurrentRoleError):
            owner.withdraw(reservation)

    asyncio.run(wrong_loop())


@pytest.mark.asyncio
async def test_connection_and_challenge_tokens_require_canonical_nonzero_uuid():
    server = context()
    owner = AuthenticatedControlRoleOwner((server,), "hub")
    for invalid in ("connection", "00000000-0000-0000-0000-000000000000"):
        with pytest.raises(CurrentRoleError):
            owner.reserve(server, invalid)
    reservation = owner.reserve(server, CONNECTION)
    with pytest.raises(CurrentRoleError):
        owner.bind_challenge(reservation, "challenge")
