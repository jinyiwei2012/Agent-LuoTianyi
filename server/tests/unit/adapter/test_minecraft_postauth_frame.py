import base64
import json
import struct

import pytest

from src.adapter.minecraft.postauth_frame import (
    PostAuthFrame,
    PostAuthFrameError,
    TrustedFrameContext,
    encode_frame,
    parse_frame,
)
from src.adapter.minecraft.postauth_keys import PostAuthSessionKeys
from tests.unit.adapter.test_minecraft_postauth_keys import CHALLENGE, PSK, transcript_data

CONNECTION = "00000000-0000-4000-8000-000000000002"
OTHER = "00000000-0000-4000-8000-000000000003"
ROUTE = "00000000-0000-4000-8000-000000000004"
BODY = "00000000-0000-4000-8000-000000000005"


def codec(role: str = "CLIENT_CONTROLLER", connection: str | None = CONNECTION):
    authenticated = transcript_data(role)
    return TrustedFrameContext(authenticated=authenticated, connection_id=connection), PostAuthSessionKeys(
        authenticated, PSK
    )


def nested_notice() -> bytes:
    return json.dumps(
        {
            "gameNonce": base64.urlsafe_b64encode(b"n" * 32).decode().rstrip("="),
            "bodyId": BODY,
            "kind": "BODY_BIND_NOTICE",
            "security": "luosandbox.control-auth.v1",
            "gameConnectionId": OTHER,
            "serverBootId": ROUTE,
        },
        separators=(",", ":"),
    ).encode()


def nested_candidate() -> bytes:
    return json.dumps(
        {
            "bodyId": BODY,
            "kind": "BODY_BOUND",
            "security": "luosandbox.control-auth.v1",
            "gameConnectionId": OTHER,
            "serverBootId": ROUTE,
        },
        separators=(",", ":"),
    ).encode()


def frame(kind: str, **changes: object) -> PostAuthFrame:
    values: dict[str, object] = {
        "connection_id": CONNECTION,
        "challenge_id": CHALLENGE,
        "sequence": 0,
        "message_kind": kind,
        "route_id": "",
        "source_install_id": "",
        "source_connection_id": "",
        "recipient_connection_id": "",
        "challenge_expires_at": "",
        "payload": b"{}",
    }
    if kind == "SESSION_OPEN":
        values["payload"] = (
            b'{"security":"luosandbox.control-auth.v1","kind":"SESSION_OPEN","connectionId":"'
            + CONNECTION.encode()
            + b'"}'
        )
    if kind in ("BODY_BIND_NOTICE", "BODY_BIND_ROUTE"):
        values["payload"] = nested_notice()
    if kind == "BODY_BIND_ROUTE":
        values.update(
            route_id=ROUTE, source_install_id="client-1", source_connection_id=OTHER, recipient_connection_id=CONNECTION
        )
    if kind == "BODY_BOUND_CANDIDATE":
        values.update(
            payload=nested_candidate(),
            route_id=ROUTE,
            recipient_connection_id=CONNECTION,
            challenge_expires_at="2026-10-23T00:00:00.000Z",
        )
    values.update(changes)
    return PostAuthFrame(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kind,role,direction",
    (
        ("SESSION_OPEN", "CLIENT_CONTROLLER", "HUB_TO_PEER"),
        ("HEARTBEAT", "CLIENT_CONTROLLER", "PEER_TO_HUB"),
        ("BODY_BIND_NOTICE", "CLIENT_CONTROLLER", "PEER_TO_HUB"),
        ("BODY_BIND_ROUTE", "SERVER_AUTHORITY", "HUB_TO_PEER"),
        ("BODY_BOUND_CANDIDATE", "SERVER_AUTHORITY", "PEER_TO_HUB"),
    ),
)
def test_all_five_kinds_round_trip_exact_semantics(kind: str, role: str, direction: str):
    trusted, keys = codec(role)
    item = frame(kind)
    raw = encode_frame(item, trusted, keys, direction)
    assert parse_frame(raw, trusted, keys, direction) == item


def test_session_open_allows_first_adoption_only_and_exact_payload():
    trusted, keys = codec(connection=None)
    raw = encode_frame(frame("SESSION_OPEN"), trusted, keys, "HUB_TO_PEER")
    assert parse_frame(raw, trusted, keys, "HUB_TO_PEER").connection_id == CONNECTION
    with pytest.raises(PostAuthFrameError):
        encode_frame(frame("HEARTBEAT"), trusted, keys, "PEER_TO_HUB")
    with pytest.raises(PostAuthFrameError):
        encode_frame(frame("SESSION_OPEN", payload=b" {}"), trusted, keys, "HUB_TO_PEER")


@pytest.mark.parametrize(
    "kind,role,direction",
    (
        ("SESSION_OPEN", "CLIENT_CONTROLLER", "PEER_TO_HUB"),
        ("BODY_BIND_NOTICE", "SERVER_AUTHORITY", "PEER_TO_HUB"),
        ("BODY_BIND_ROUTE", "CLIENT_CONTROLLER", "HUB_TO_PEER"),
        ("BODY_BOUND_CANDIDATE", "CLIENT_CONTROLLER", "PEER_TO_HUB"),
    ),
)
def test_wrong_kind_role_direction_combinations_reject(kind: str, role: str, direction: str):
    trusted, keys = codec(role)
    with pytest.raises(PostAuthFrameError):
        encode_frame(frame(kind), trusted, keys, direction)


def test_outer_json_structure_type_and_mac_field_tampering_rejects():
    trusted, keys = codec()
    raw = encode_frame(frame("HEARTBEAT"), trusted, keys, "PEER_TO_HUB")
    parsed = json.loads(raw)
    for name in parsed:
        changed = dict(parsed)
        if name == "sequence":
            changed[name] = 1
        elif name == "mac":
            changed[name] = "A" * 43
        else:
            changed[name] = "tampered"
        with pytest.raises(PostAuthFrameError):
            parse_frame(json.dumps(changed, separators=(",", ":")).encode(), trusted, keys, "PEER_TO_HUB")
    invalid = (
        b"\xef\xbb\xbf" + raw,
        raw + b"x",
        raw.replace(b'"security"', b'"extra"', 1),
        raw.replace(b"{", b'{"security":"x",', 1),
        raw.replace(b'"sequence":0', b'"sequence":true', 1),
        raw.replace(b'"sequence":0', b'"sequence":0.0', 1),
    )
    for item in invalid:
        with pytest.raises(PostAuthFrameError):
            parse_frame(item, trusted, keys, "PEER_TO_HUB")


@pytest.mark.parametrize("value", ("bad id", "bad\t", "bad@", "-bad", "a" * 129))
def test_ids_and_uuids_are_strict(value: str):
    trusted, keys = codec("SERVER_AUTHORITY")
    with pytest.raises(PostAuthFrameError):
        encode_frame(frame("BODY_BIND_ROUTE", source_install_id=value), trusted, keys, "HUB_TO_PEER")
    with pytest.raises(PostAuthFrameError):
        encode_frame(frame("HEARTBEAT", connection_id=value), trusted, keys, "PEER_TO_HUB")


def test_base64_nested_schema_and_expiry_are_strict_but_nested_whitespace_is_allowed():
    trusted, keys = codec()
    valid = frame("BODY_BIND_NOTICE", payload=b"  " + nested_notice() + b" \n")
    assert parse_frame(encode_frame(valid, trusted, keys, "PEER_TO_HUB"), trusted, keys, "PEER_TO_HUB") == valid
    for payload in (
        nested_notice().replace(b"gameNonce", b"unknown"),
        nested_notice().replace(b'"gameNonce":"', b'"gameNonce":"=', 1),
        b"{}",
    ):
        with pytest.raises(PostAuthFrameError):
            encode_frame(frame("BODY_BIND_NOTICE", payload=payload), trusted, keys, "PEER_TO_HUB")
    server, server_keys = codec("SERVER_AUTHORITY")
    for expiry in ("2026-10-23T00:00:00Z", "2026-10-23T00:00:00.000+00:00", "2026-99-23T00:00:00.000Z"):
        with pytest.raises(PostAuthFrameError):
            encode_frame(frame("BODY_BOUND_CANDIDATE", challenge_expires_at=expiry), server, server_keys, "PEER_TO_HUB")


def test_size_caps_and_closed_keys_reject_without_state_claims():
    trusted, keys = codec()
    with pytest.raises(PostAuthFrameError):
        encode_frame(frame("HEARTBEAT", payload=b"x" * 2049), trusted, keys, "PEER_TO_HUB")
    raw = encode_frame(frame("HEARTBEAT"), trusted, keys, "PEER_TO_HUB")
    with pytest.raises(PostAuthFrameError):
        parse_frame(raw + b" " * (4097 - len(raw)), trusted, keys, "PEER_TO_HUB")
    keys.close()
    with pytest.raises(Exception):
        encode_frame(frame("HEARTBEAT"), trusted, keys, "PEER_TO_HUB")


def test_mac_input_is_exactly_18_length_prefixed_fields_with_u64_sequence():
    from src.adapter.minecraft import postauth_frame

    trusted, _ = codec()
    item = frame("HEARTBEAT", sequence=7)
    raw = postauth_frame._mac_input(item, trusted, "PEER_TO_HUB")
    fields = []
    offset = 0
    while offset < len(raw):
        length = struct.unpack(">I", raw[offset : offset + 4])[0]
        offset += 4
        fields.append(raw[offset : offset + length])
        offset += length
    assert offset == len(raw) and len(fields) == 18
    assert fields[10] == struct.pack(">Q", 7)
    expected = (
        b"LUO_CONTROL_FRAME_V2",
        b"luosandbox.control-auth.v1",
        b"AUTHENTICATED_FRAME",
        b"LUO_CONTROL_POSTAUTH_V2",
        b"CLIENT_CONTROLLER",
        b"client-1",
        b"hub-1",
        CONNECTION.encode(),
        CHALLENGE.encode(),
        b"PEER_TO_HUB",
        struct.pack(">Q", 7),
        b"HEARTBEAT",
        b"",
        b"",
        b"",
        b"",
        b"",
        b"{}",
    )
    assert raw == b"".join(struct.pack(">I", len(value)) + value for value in expected)


def test_context_binding_and_deep_json_fail_closed_without_recursion_error():
    trusted, keys = codec()
    wrong_auth = transcript_data(config_hash=b"d" * 32)
    wrong_context = TrustedFrameContext(authenticated=wrong_auth, connection_id=CONNECTION)
    wrong_keys = PostAuthSessionKeys(wrong_auth, PSK)
    raw = encode_frame(frame("HEARTBEAT"), trusted, keys, "PEER_TO_HUB")
    with pytest.raises(PostAuthFrameError):
        parse_frame(raw, wrong_context, keys, "PEER_TO_HUB")
    with pytest.raises(PostAuthFrameError):
        parse_frame(raw, trusted, wrong_keys, "PEER_TO_HUB")
    malformed = b"[" * 1000 + b"]" * 1000
    with pytest.raises(PostAuthFrameError):
        parse_frame(malformed, trusted, keys, "PEER_TO_HUB")


@pytest.mark.parametrize("expiry", ("٢٠٢٦-10-23T00:00:00.000Z", "２０２６-10-23T00:00:00.000Z"))
def test_candidate_expiry_rejects_non_ascii_decimal_digits_on_encode_and_decode(expiry: str):
    trusted, keys = codec("SERVER_AUTHORITY")
    invalid = frame("BODY_BOUND_CANDIDATE", challenge_expires_at=expiry)
    with pytest.raises(PostAuthFrameError):
        encode_frame(invalid, trusted, keys, "PEER_TO_HUB")
    valid = encode_frame(frame("BODY_BOUND_CANDIDATE"), trusted, keys, "PEER_TO_HUB")
    text = valid.decode().replace("2026-10-23T00:00:00.000Z", expiry)
    with pytest.raises(PostAuthFrameError):
        parse_frame(text.encode(), trusted, keys, "PEER_TO_HUB")
