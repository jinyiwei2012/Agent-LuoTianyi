import hmac
import struct
from datetime import datetime, timedelta, timezone
from hashlib import sha256

import pytest

from src.adapter.minecraft.control_auth import ControlAuthContext, ControlAuthError, transcript
from src.adapter.minecraft.postauth_keys import AuthenticatedTranscript, PostAuthKeyError, PostAuthSessionKeys

PSK = b"k" * 32
CHALLENGE = "00000000-0000-4000-8000-000000000001"


def transcript_data(role: str = "CLIENT_CONTROLLER", **changes: object) -> AuthenticatedTranscript:
    challenge_id = changes.pop("challenge_id", CHALLENGE)
    peer_nonce = changes.pop("peer_nonce", b"p" * 32)
    hub_nonce = changes.pop("hub_nonce", b"h" * 32)
    issued_at = changes.pop("issued_at", datetime(2026, 10, 23, tzinfo=timezone.utc))
    expires_at = changes.pop("expires_at", issued_at + timedelta(seconds=5))
    values: dict[str, object] = {
        "peer_role": role,
        "peer_install_id": "client-1",
        "hub_install_id": "hub-1",
        "key_id": "key-1",
        "protocol": "LUO_CONTROL_POSTAUTH_V2",
        "mod_hash": b"m" * 32,
        "config_hash": b"c" * 32,
        "binding_hash": b"b" * 32,
    }
    values.update(changes)
    context = ControlAuthContext(**values)  # type: ignore[arg-type]
    return AuthenticatedTranscript(
        context=context,
        peer_nonce=peer_nonce,  # type: ignore[arg-type]
        hub_nonce=hub_nonce,  # type: ignore[arg-type]
        challenge_id=challenge_id,  # type: ignore[arg-type]
        issued_at=issued_at,  # type: ignore[arg-type]
        expires_at=expires_at,  # type: ignore[arg-type]
    )


def manual_key(authenticated: AuthenticatedTranscript, direction: str) -> bytes:
    value = authenticated
    digest = sha256(
        transcript(
            "PEER_PROOF",
            value.context,
            value.peer_nonce,
            value.hub_nonce,
            value.challenge_id,
            value.issued_at,
            value.expires_at,
        )
    ).digest()
    fields = (b"LUO_CONTROL_SESSION_KEY_V2", digest, direction.encode("ascii"))
    encoded = b"".join(struct.pack(">I", len(field)) + field for field in fields)
    return hmac.new(PSK, encoded, sha256).digest()


@pytest.mark.parametrize("role", ("CLIENT_CONTROLLER", "SERVER_AUTHORITY"))
@pytest.mark.parametrize("direction", ("PEER_TO_HUB", "HUB_TO_PEER"))
def test_keys_match_independent_existing_transcript_kdf(role: str, direction: str):
    authenticated = transcript_data(role)
    keys = PostAuthSessionKeys(authenticated, PSK)
    assert (
        keys.mac(direction, b"known frame")
        == hmac.new(manual_key(authenticated, direction), b"known frame", sha256).digest()
    )


def test_direction_isolated_context_bound_and_close_erases_private_bytearrays():
    authenticated = transcript_data()
    keys = PostAuthSessionKeys(authenticated, PSK)
    message = b"frame"
    peer = keys.mac("PEER_TO_HUB", message)
    assert peer != keys.mac("HUB_TO_PEER", message)
    assert not PostAuthSessionKeys(transcript_data(peer_install_id="client-2"), PSK).verify(
        "PEER_TO_HUB", message, peer
    )
    assert not PostAuthSessionKeys(authenticated, b"z" * 32).verify("PEER_TO_HUB", message, peer)
    private = tuple(keys._keys.values())
    assert "6b" * 32 not in repr(keys) and not hasattr(keys, "key")
    keys.close()
    assert all(value == bytearray(32) for value in private)
    assert not keys.verify("PEER_TO_HUB", message, peer)
    with pytest.raises(PostAuthKeyError):
        keys.mac("PEER_TO_HUB", message)


@pytest.mark.parametrize(
    "change",
    (
        {"role": "SERVER_AUTHORITY"},
        {"peer_install_id": "client-2"},
        {"hub_install_id": "hub-2"},
        {"key_id": "key-2"},
        {"challenge_id": "00000000-0000-4000-8000-000000000009"},
        {"peer_nonce": b"q" * 32},
        {"hub_nonce": b"i" * 32},
        {"mod_hash": b"o" * 32},
        {"config_hash": b"d" * 32},
        {"binding_hash": b"e" * 32},
        {"issued_at": datetime(2026, 10, 23, 0, 0, 1, tzinfo=timezone.utc)},
        {"expires_at": datetime(2026, 10, 23, 0, 0, 6, tzinfo=timezone.utc)},
    ),
)
def test_full_authenticated_transcript_binding_rejects_even_a_locally_matching_key(change: dict[str, object]):
    keys = PostAuthSessionKeys(transcript_data(), PSK)
    altered = transcript_data(**change)
    locally_matching = PostAuthSessionKeys(altered, PSK)
    keys.require_context(transcript_data())
    with pytest.raises(PostAuthKeyError):
        keys.require_context(altered)
    assert locally_matching.verify("PEER_TO_HUB", b"x", locally_matching.mac("PEER_TO_HUB", b"x"))


@pytest.mark.parametrize(
    "change",
    (
        {"peer_install_id": "client-2"},
        {"hub_install_id": "hub-2"},
        {"challenge_id": "00000000-0000-4000-8000-000000000009"},
    ),
)
def test_context_mutations_change_derived_mac(change: dict[str, object]):
    baseline = PostAuthSessionKeys(transcript_data(), PSK).mac("PEER_TO_HUB", b"x")
    assert PostAuthSessionKeys(transcript_data(**change), PSK).mac("PEER_TO_HUB", b"x") != baseline


def test_rejects_non_v2_or_non_peer_context_and_invalid_psk():
    with pytest.raises(PostAuthKeyError):
        transcript_data(protocol="luosandbox.minecraft.v1")
    with pytest.raises(PostAuthKeyError):
        PostAuthSessionKeys(transcript_data(), bytearray(PSK))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "invalid",
    ("space id", 'quote"', "back\\slash", "tab\t", "line\n", "control\x1f", "-leading", "_leading", "a" * 129),
)
@pytest.mark.parametrize("field", ("peer_install_id", "hub_install_id", "key_id"))
def test_v2_transcript_rejects_noncanonical_identity_fields(field: str, invalid: str):
    with pytest.raises((ControlAuthError, PostAuthKeyError)):
        transcript_data(**{field: invalid})


def test_v2_transcript_accepts_one_and_128_byte_ids_with_allowed_punctuation():
    maximum = "a" + "A9._:/-" * 18 + "A"
    assert len(maximum) == 128
    authenticated = transcript_data(peer_install_id="a", hub_install_id=maximum, key_id=maximum)
    assert PostAuthSessionKeys(authenticated, PSK).verify(
        "PEER_TO_HUB", b"x", PostAuthSessionKeys(authenticated, PSK).mac("PEER_TO_HUB", b"x")
    )


@pytest.mark.parametrize(
    "challenge",
    (
        "00000000-0000-0000-0000-000000000000",
        "00000000-0000-4000-8000-00000000000A",
        "00000000000040008000000000000001",
    ),
)
def test_v2_transcript_rejects_noncanonical_or_zero_challenge(challenge: str):
    with pytest.raises(PostAuthKeyError):
        transcript_data(challenge_id=challenge)
