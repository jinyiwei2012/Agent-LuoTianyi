"""Strict, pure v2 post-auth frame codec with no session ownership semantics."""

from __future__ import annotations

import base64
import json
import re
import struct
from dataclasses import dataclass
from datetime import datetime, timezone

from .postauth_keys import AuthenticatedTranscript, PostAuthSessionKeys

_KEYS = (
    "security",
    "kind",
    "profile",
    "connectionId",
    "challengeId",
    "sequence",
    "messageKind",
    "routeId",
    "sourceInstallId",
    "sourceConnectionId",
    "recipientConnectionId",
    "challengeExpiresAt",
    "payload",
    "mac",
)
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z")
_B64 = re.compile(r"[A-Za-z0-9_-]*")
_I63 = (1 << 63) - 1
_PEER_TO_HUB = "PEER_TO_HUB"
_HUB_TO_PEER = "HUB_TO_PEER"


class PostAuthFrameError(ValueError):
    """Raised for syntactically invalid or unauthenticated frame bytes."""


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class TrustedFrameContext:
    """Caller-supplied authenticated expectations, not payload-derived identity."""

    authenticated: AuthenticatedTranscript
    connection_id: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.authenticated, AuthenticatedTranscript) or (
            self.connection_id is not None and not _uuid(self.connection_id)
        ):
            raise PostAuthFrameError("invalid trusted frame context")


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class PostAuthFrame:
    """Validated frame evidence only; it grants no transport or world authority."""

    connection_id: str
    challenge_id: str
    sequence: int
    message_kind: str
    route_id: str
    source_install_id: str
    source_connection_id: str
    recipient_connection_id: str
    challenge_expires_at: str
    payload: bytes


def encode_frame(
    frame: PostAuthFrame, context: TrustedFrameContext, keys: PostAuthSessionKeys, direction: str
) -> bytes:
    _require_keys_context(keys, context)
    _validate_frame(frame, context, direction)
    payload = _b64(frame.payload)
    values = _values(frame, payload, "")
    mac = _b64(keys.mac(direction, _mac_input(frame, context, direction)))
    values["mac"] = mac
    raw = json.dumps(values, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    if len(raw) > 4096:
        raise PostAuthFrameError("frame too large")
    return raw


def parse_frame(raw: bytes, context: TrustedFrameContext, keys: PostAuthSessionKeys, direction: str) -> PostAuthFrame:
    _require_keys_context(keys, context)
    if type(raw) is not bytes or not raw or len(raw) > 4096 or raw.startswith(b"\xef\xbb\xbf"):
        raise PostAuthFrameError("invalid frame bytes")
    try:
        _check_json_depth(raw)
        text = raw.decode("utf-8")
        values = json.loads(text, object_pairs_hook=_object, parse_constant=_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, PostAuthFrameError) as error:
        raise PostAuthFrameError("invalid frame JSON") from error
    if type(values) is not dict or set(values) != set(_KEYS):
        raise PostAuthFrameError("invalid frame keys")
    if (
        values["security"] != "luosandbox.control-auth.v1"
        or values["kind"] != "AUTHENTICATED_FRAME"
        or values["profile"] != "LUO_CONTROL_POSTAUTH_V2"
    ):
        raise PostAuthFrameError("invalid frame constants")
    if any(type(values[key]) is not str for key in _KEYS if key != "sequence") or type(values["sequence"]) is not int:
        raise PostAuthFrameError("invalid frame field type")
    try:
        payload = _unb64(values["payload"])
        frame = PostAuthFrame(
            connection_id=values["connectionId"],
            challenge_id=values["challengeId"],
            sequence=values["sequence"],
            message_kind=values["messageKind"],
            route_id=values["routeId"],
            source_install_id=values["sourceInstallId"],
            source_connection_id=values["sourceConnectionId"],
            recipient_connection_id=values["recipientConnectionId"],
            challenge_expires_at=values["challengeExpiresAt"],
            payload=payload,
        )
        _validate_frame(frame, context, direction)
        supplied = _unb64(values["mac"])
    except (KeyError, TypeError, PostAuthFrameError) as error:
        raise PostAuthFrameError("invalid frame") from error
    if len(supplied) != 32 or not keys.verify(direction, _mac_input(frame, context, direction), supplied):
        raise PostAuthFrameError("invalid frame MAC")
    return frame


def _validate_frame(frame: PostAuthFrame, context: TrustedFrameContext, direction: str) -> None:
    if (
        not isinstance(frame, PostAuthFrame)
        or not isinstance(context, TrustedFrameContext)
        or direction not in (_PEER_TO_HUB, _HUB_TO_PEER)
    ):
        raise PostAuthFrameError("invalid frame context")
    if (
        not _uuid(frame.connection_id)
        or not _uuid(frame.challenge_id)
        or frame.challenge_id != context.authenticated.challenge_id
    ):
        raise PostAuthFrameError("invalid connection or challenge")
    if context.connection_id is not None and frame.connection_id != context.connection_id:
        raise PostAuthFrameError("unexpected connection")
    if (
        type(frame.sequence) is not int
        or not 0 <= frame.sequence <= _I63
        or not _ID.fullmatch(frame.message_kind)
        or type(frame.payload) is not bytes
        or len(frame.payload) > 2048
    ):
        raise PostAuthFrameError("invalid frame values")
    if context.connection_id is None and frame.message_kind != "SESSION_OPEN":
        raise PostAuthFrameError("connection adoption requires session open")
    _validate_semantics(frame, context, direction)


def _validate_semantics(frame: PostAuthFrame, context: TrustedFrameContext, direction: str) -> None:  # noqa: C901
    role = context.authenticated.context.peer_role
    empty = (
        frame.route_id,
        frame.source_install_id,
        frame.source_connection_id,
        frame.recipient_connection_id,
        frame.challenge_expires_at,
    )
    if frame.message_kind == "SESSION_OPEN":
        expected = (
            b'{"security":"luosandbox.control-auth.v1","kind":"SESSION_OPEN","connectionId":"'
            + frame.connection_id.encode()
            + b'"}'
        )
        if direction != _HUB_TO_PEER or frame.sequence != 0 or any(empty) or frame.payload != expected:
            raise PostAuthFrameError("invalid session open")
    elif frame.message_kind == "HEARTBEAT":
        if any(empty) or frame.payload != b"{}":
            raise PostAuthFrameError("invalid heartbeat")
    elif frame.message_kind == "BODY_BIND_NOTICE":
        if direction != _PEER_TO_HUB or role != "CLIENT_CONTROLLER" or any(empty):
            raise PostAuthFrameError("invalid body notice")
        _nested(
            frame.payload,
            ("security", "kind", "serverBootId", "gameConnectionId", "bodyId", "gameNonce"),
            "BODY_BIND_NOTICE",
        )
    elif frame.message_kind == "BODY_BIND_ROUTE":
        if (
            direction != _HUB_TO_PEER
            or role != "SERVER_AUTHORITY"
            or not _uuid(frame.route_id)
            or not _ID.fullmatch(frame.source_install_id)
            or not _uuid(frame.source_connection_id)
            or frame.recipient_connection_id != frame.connection_id
            or frame.challenge_expires_at
        ):
            raise PostAuthFrameError("invalid body route")
        _nested(
            frame.payload,
            ("security", "kind", "serverBootId", "gameConnectionId", "bodyId", "gameNonce"),
            "BODY_BIND_NOTICE",
        )
    elif frame.message_kind == "BODY_BOUND_CANDIDATE":
        if (
            direction != _PEER_TO_HUB
            or role != "SERVER_AUTHORITY"
            or not _uuid(frame.route_id)
            or frame.source_install_id
            or frame.source_connection_id
            or frame.recipient_connection_id != frame.connection_id
            or not _utc(frame.challenge_expires_at)
        ):
            raise PostAuthFrameError("invalid body candidate")
        _nested(frame.payload, ("security", "kind", "serverBootId", "gameConnectionId", "bodyId"), "BODY_BOUND")
    else:
        raise PostAuthFrameError("unknown frame kind")


def _nested(raw: bytes, expected: tuple[str, ...], kind: str) -> None:
    try:
        _check_json_depth(raw)
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)
    except (RecursionError, UnicodeDecodeError, json.JSONDecodeError, PostAuthFrameError) as error:
        raise PostAuthFrameError("invalid nested payload") from error
    if (
        type(value) is not dict
        or set(value) != set(expected)
        or any(type(item) is not str for item in value.values())
        or value["security"] != "luosandbox.control-auth.v1"
        or value["kind"] != kind
        or not all(_uuid(value[key]) for key in ("serverBootId", "gameConnectionId", "bodyId"))
    ):
        raise PostAuthFrameError("invalid nested payload")
    if "gameNonce" in value and (not _B64.fullmatch(value["gameNonce"]) or len(_unb64(value["gameNonce"])) != 32):
        raise PostAuthFrameError("invalid nonce")


def _values(frame: PostAuthFrame, payload: str, mac: str) -> dict[str, object]:
    return {
        "security": "luosandbox.control-auth.v1",
        "kind": "AUTHENTICATED_FRAME",
        "profile": "LUO_CONTROL_POSTAUTH_V2",
        "connectionId": frame.connection_id,
        "challengeId": frame.challenge_id,
        "sequence": frame.sequence,
        "messageKind": frame.message_kind,
        "routeId": frame.route_id,
        "sourceInstallId": frame.source_install_id,
        "sourceConnectionId": frame.source_connection_id,
        "recipientConnectionId": frame.recipient_connection_id,
        "challengeExpiresAt": frame.challenge_expires_at,
        "payload": payload,
        "mac": mac,
    }


def _mac_input(frame: PostAuthFrame, context: TrustedFrameContext, direction: str) -> bytes:
    fields = (
        b"LUO_CONTROL_FRAME_V2",
        b"luosandbox.control-auth.v1",
        b"AUTHENTICATED_FRAME",
        b"LUO_CONTROL_POSTAUTH_V2",
        context.authenticated.context.peer_role.encode(),
        context.authenticated.context.peer_install_id.encode(),
        context.authenticated.context.hub_install_id.encode(),
        frame.connection_id.encode(),
        frame.challenge_id.encode(),
        direction.encode(),
        struct.pack(">Q", frame.sequence),
        frame.message_kind,
        frame.route_id,
        frame.source_install_id,
        frame.source_connection_id,
        frame.recipient_connection_id,
        frame.challenge_expires_at,
        frame.payload,
    )
    encoded = tuple(value if isinstance(value, bytes) else value.encode("ascii") for value in fields)
    return b"".join(struct.pack(">I", len(value)) + value for value in encoded)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    if not _B64.fullmatch(value) or "=" in value:
        raise PostAuthFrameError("invalid base64url")
    try:
        result = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, UnicodeError) as error:
        raise PostAuthFrameError("invalid base64url") from error
    if _b64(result) != value:
        raise PostAuthFrameError("noncanonical base64url")
    return result


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PostAuthFrameError("duplicate key")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise PostAuthFrameError(f"invalid constant {value}")


def _uuid(value: object) -> bool:
    return type(value) is str and _UUID.fullmatch(value) is not None and value != "00000000-0000-0000-0000-000000000000"


def _utc(value: object) -> bool:
    if type(value) is not str or _UTC.fullmatch(value) is None:
        return False
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).microsecond % 1000 == 0
    except ValueError:
        return False


def _require_keys_context(keys: object, context: object) -> None:
    if not isinstance(keys, PostAuthSessionKeys) or not isinstance(context, TrustedFrameContext):
        raise PostAuthFrameError("invalid frame cryptographic context")
    try:
        keys.require_context(context.authenticated)
    except Exception as error:
        raise PostAuthFrameError("invalid frame cryptographic context") from error


def _check_json_depth(raw: bytes) -> None:  # noqa: C901
    """Bound JSON nesting before decoding; quoted bytes do not affect structural depth."""
    depth = 0
    quoted = False
    escaped = False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == ord("\\"):
                escaped = True
            elif byte == ord('"'):
                quoted = False
            continue
        if byte == ord('"'):
            quoted = True
        elif byte in (ord("{"), ord("[")):
            depth += 1
            if depth > 4:
                raise PostAuthFrameError("JSON nesting too deep")
        elif byte in (ord("}"), ord("]")):
            depth -= 1
            if depth < 0:
                raise PostAuthFrameError("invalid JSON structure")
