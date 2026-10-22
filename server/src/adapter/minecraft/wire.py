"""Strict, transport-neutral decoder for frozen Minecraft contract v1 bytes.

This module deserializes canonical messages only. It does not authenticate a
connection, bind a body, assess freshness, or perform admission/execution.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class WireErrorCategory(str, Enum):
    LEXICAL = "LEXICAL"
    STRUCTURE = "STRUCTURE"
    SEMANTIC = "SEMANTIC"


class MinecraftWireError(ValueError):
    def __init__(self, category: WireErrorCategory, message: str) -> None:
        super().__init__(message)
        self.category = category


@dataclass(frozen=True, slots=True)
class MinecraftEnvelope:
    protocol: str
    type: str
    actor_id: str
    world_id: str
    body_id: str
    session_id: str
    session_epoch: int
    message_id: str
    timestamp: datetime
    payload: Mapping[str, Any]


class _JsonNumber(Decimal):
    """Preserves that a value arrived through JSON number syntax."""


_IDS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_UUID = re.compile(
    r"^(?!00000000-0000-0000-0000-000000000000$)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_UTC = re.compile(r"^(?!0000)(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d{3})Z$")
_TYPES = frozenset(
    ("observation", "request", "admission", "cancel", "cancel_result", "receipt", "reconcile", "reconcile_result")
)
_REASONS = frozenset(
    (
        "BINDING_MISMATCH",
        "STALE_EPOCH",
        "DEADLINE_EXPIRED",
        "ID_CONFLICT",
        "RATE_LIMIT",
        "BACKPRESSURE",
        "STALE_BASIS",
        "AUDIENCE_DENIED",
        "CAPABILITY_UNAVAILABLE",
        "REGION_DENIED",
        "UNSAFE_TARGET",
        "BODY_BUSY",
        "QUARANTINED",
        "TARGET_TERMINAL",
        "TARGET_NOT_FOUND",
        "OUTCOME_UNPROVEN",
        "HISTORY_UNAVAILABLE",
        "QUIESCENCE_UNPROVEN",
        "STUCK",
        "EXECUTION_REJECTED",
        "CANCEL_CONFIRMED",
        "CONTROL_LOST",
    )
)
_I63_MAX = 9223372036854775807
_GENERIC_REJECT_REASONS = frozenset(
    ("BINDING_MISMATCH", "STALE_EPOCH", "DEADLINE_EXPIRED", "ID_CONFLICT", "RATE_LIMIT", "BACKPRESSURE")
)
_MAX_FRAME = 65536
_MAX_DEPTH = 12
_MAX_TOKENS = 2048
_MAX_NAME = 64
_MAX_STRING = 16384
_MAX_NUMBER = 64


def decode_message(data: bytes) -> MinecraftEnvelope:
    """Decode a capped UTF-8 JSON object with duplicate keys rejected at every depth."""
    if not isinstance(data, bytes) or len(data) > _MAX_FRAME:
        raise MinecraftWireError(WireErrorCategory.LEXICAL, "frame exceeds byte limit")
    if data.startswith(b"\xef\xbb\xbf"):
        raise MinecraftWireError(WireErrorCategory.LEXICAL, "BOM is forbidden")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise MinecraftWireError(WireErrorCategory.LEXICAL, "invalid UTF-8") from error
    if _has_surrogate(text):
        raise MinecraftWireError(WireErrorCategory.LEXICAL, "surrogate is forbidden")
    _preflight_json(text)
    try:
        value = json.loads(
            text, object_pairs_hook=_object, parse_int=_integer, parse_float=_decimal, parse_constant=_constant
        )
    except MinecraftWireError:
        raise
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError, OverflowError) as error:
        raise MinecraftWireError(WireErrorCategory.LEXICAL, "invalid JSON") from error
    _depth(value)
    if _contains_surrogate(value):
        raise MinecraftWireError(WireErrorCategory.LEXICAL, "surrogate is forbidden")
    try:
        return _envelope(value)
    except MinecraftWireError:
        raise
    except (TypeError, ValueError, OverflowError, KeyError) as error:
        raise MinecraftWireError(WireErrorCategory.SEMANTIC, "invalid message") from error


def encode_message(envelope: MinecraftEnvelope) -> bytes:
    """Encode a normalized envelope; this is not an authentication transcript."""
    value = {
        "protocol": envelope.protocol,
        "type": envelope.type,
        "actorId": envelope.actor_id,
        "worldId": envelope.world_id,
        "bodyId": envelope.body_id,
        "sessionId": envelope.session_id,
        "sessionEpoch": envelope.session_epoch,
        "messageId": envelope.message_id,
        "timestamp": _format_utc(envelope.timestamp),
        "payload": _thaw(envelope.payload),
    }
    try:
        _envelope(value)
        encoded = json.dumps(_json_numbers(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    except MinecraftWireError:
        raise
    except (TypeError, ValueError, OverflowError) as error:
        raise MinecraftWireError(WireErrorCategory.SEMANTIC, "invalid envelope") from error
    if len(encoded) > _MAX_FRAME:
        raise MinecraftWireError(WireErrorCategory.LEXICAL, "frame exceeds byte limit")
    return encoded


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MinecraftWireError(WireErrorCategory.LEXICAL, "duplicate JSON key")
        result[key] = value
    return result


def _integer(value: str) -> int:
    if len(value) > _MAX_NUMBER:
        raise MinecraftWireError(WireErrorCategory.LEXICAL, "number exceeds limit")
    return int(value)


def _decimal(value: str) -> _JsonNumber:
    if len(value) > _MAX_NUMBER:
        raise MinecraftWireError(WireErrorCategory.LEXICAL, "number exceeds limit")
    return _JsonNumber(value)


def _constant(_: str) -> None:
    raise MinecraftWireError(WireErrorCategory.LEXICAL, "non-finite JSON number")


def _depth(value: Any, level: int = 0) -> None:
    if level > _MAX_DEPTH:
        raise MinecraftWireError(WireErrorCategory.LEXICAL, "JSON depth exceeds limit")
    if isinstance(value, dict):
        for item in value.values():
            _depth(item, level + 1)
    elif isinstance(value, list):
        for item in value:
            _depth(item, level + 1)


def _preflight_json(text: str) -> None:  # noqa: C901 - single-pass lexical limiter avoids a second parser
    """Bound tokens and lexical runs before stdlib JSON allocates nested values."""
    depth = tokens = index = 0
    expecting_name = False
    while index < len(text):
        char = text[index]
        if char in " \t\r\n,:":
            index += 1
            continue
        if char in "[{":
            depth += 1
            tokens += 1
            expecting_name = char == "{"
            index += 1
        elif char in "]}":
            depth -= 1
            index += 1
        elif char == '"':
            start = index
            index += 1
            escaped = False
            while index < len(text):
                current = text[index]
                if current == '"' and not escaped:
                    break
                escaped = current == "\\" and not escaped
                if current != "\\":
                    escaped = False
                index += 1
            if index >= len(text):
                raise MinecraftWireError(WireErrorCategory.LEXICAL, "unterminated string")
            size = index - start - 1
            if size > (_MAX_NAME if expecting_name else _MAX_STRING):
                raise MinecraftWireError(WireErrorCategory.LEXICAL, "string exceeds limit")
            tokens += 1
            expecting_name = False
            index += 1
        else:
            start = index
            while index < len(text) and text[index] not in " \t\r\n,]}":
                index += 1
            if index - start > _MAX_NUMBER:
                raise MinecraftWireError(WireErrorCategory.LEXICAL, "number exceeds limit")
            tokens += 1
        if depth > _MAX_DEPTH or depth < 0 or tokens > _MAX_TOKENS:
            raise MinecraftWireError(WireErrorCategory.LEXICAL, "JSON limit exceeded")


def _envelope(value: Any) -> MinecraftEnvelope:
    obj = _obj(
        value,
        {
            "protocol",
            "type",
            "actorId",
            "worldId",
            "bodyId",
            "sessionId",
            "sessionEpoch",
            "messageId",
            "timestamp",
            "payload",
        },
    )
    if obj["protocol"] != "luosandbox.minecraft.v1" or obj["type"] not in _TYPES:
        _semantic("unsupported protocol or type")
    _id(obj["actorId"])
    _id(obj["worldId"])
    for name in ("bodyId", "sessionId", "messageId"):
        _uuid(obj[name])
    _i63(obj["sessionEpoch"])
    timestamp = _utc(obj["timestamp"])
    payload = _payload(obj["type"], obj["payload"])
    if obj["type"] in {"request", "cancel", "reconcile"}:
        deadline = _utc(payload["deadline"])
        delta = (deadline - timestamp).total_seconds() * 1000
        if not 0 < delta <= 5000:
            _semantic("deadline is outside local envelope bounds")
    if obj["type"] == "cancel" and payload["targetRequestId"] == obj["messageId"]:
        _semantic("cancel cannot target itself")
    if obj["type"] in {"admission", "receipt"} and payload["requestId"] == obj["messageId"]:
        _semantic("response cannot reuse request ID")
    if obj["type"] == "cancel_result" and payload["cancelId"] == obj["messageId"]:
        _semantic("response cannot reuse cancel ID")
    if obj["type"] == "reconcile_result" and payload["reconcileId"] == obj["messageId"]:
        _semantic("response cannot reuse reconcile ID")
    return MinecraftEnvelope(
        obj["protocol"],
        obj["type"],
        obj["actorId"],
        obj["worldId"],
        obj["bodyId"],
        obj["sessionId"],
        obj["sessionEpoch"],
        obj["messageId"],
        timestamp,
        _freeze(payload),
    )


def _payload(kind: str, value: Any) -> dict[str, Any]:
    if kind == "observation":
        return _observation(value)
    if kind == "request":
        return _request(value)
    if kind == "admission":
        return _admission(value)
    if kind == "cancel":
        return _cancel(value)
    if kind == "cancel_result":
        return _cancel_result(value)
    if kind == "receipt":
        return _receipt(value)
    if kind == "reconcile":
        return _reconcile(value)
    return _reconcile_result(value)


def _observation(v: Any) -> dict[str, Any]:
    o = _obj(v, {"stamp", "audience", "source", "freshness", "state", "capabilities", "untrustedText"})
    if (
        o["audience"] != "PUBLIC"
        or o["source"] != "SERVER_AUTHORITATIVE"
        or o["freshness"] not in {"CURRENT", "STALE", "UNKNOWN"}
    ):
        _semantic("invalid observation labels")
    _stamp(o["stamp"])
    _state(o["state"])
    c = _obj(o["capabilities"], {"QUERY", "PUBLIC_SAY", "LOOK", "BOUNDED_WALK", "STOP"})
    if any(x not in {"AVAILABLE", "UNAVAILABLE", "UNKNOWN"} for x in c.values()):
        _semantic("invalid capability")
    if not isinstance(o["untrustedText"], list) or len(o["untrustedText"]) > 8:
        _semantic("invalid text items")
    for item in o["untrustedText"]:
        x = _obj(item, {"eventId", "text"})
        _uuid(x["eventId"])
        _text(x["text"], 1024, 4096, nul=True)
    return o


def _request(v: Any) -> dict[str, Any]:  # noqa: C901 - closed four-intent union is clearest here
    o = _obj(v, {"deadline", "basis", "intent"})
    _utc(o["deadline"])
    if o["basis"] is not None:
        _basis(o["basis"])
    i = o["intent"]
    if not isinstance(i, dict):
        _structure("intent must be object")
    kind = i.get("kind")
    fields = {
        "QUERY": {"kind", "queryKind"},
        "PUBLIC_SAY": {"kind", "message"},
        "LOOK": {"kind", "dimensionId", "target"},
        "BOUNDED_WALK": {"kind", "dimensionId", "target", "maximumDistance"},
    }
    if kind not in fields:
        _semantic("invalid intent kind")
    if kind != "QUERY" and o["basis"] is None:
        _semantic("effect intent requires basis")
    _obj(i, fields[kind])
    if kind == "QUERY" and i["queryKind"] != "READ_ONLY_SUMMARY":
        _semantic("invalid query kind")
    if kind == "PUBLIC_SAY":
        _text(i["message"], 256, 1024, nul=True)
        if "\r" in i["message"] or "\n" in i["message"] or i["message"].lstrip().startswith("/"):
            _semantic("invalid public message")
    if kind in {"LOOK", "BOUNDED_WALK"}:
        _dimension(i["dimensionId"])
        _position(i["target"])
    if kind == "BOUNDED_WALK":
        d = _f64(i["maximumDistance"])
        if not 0 < d <= 4:
            _semantic("invalid walk distance")
        i["maximumDistance"] = d
    return o


def _admission(v: Any) -> dict[str, Any]:
    o = _obj(v, {"requestId", "decision", "reason", "stamp"})
    _uuid(o["requestId"])
    _stamp(o["stamp"])
    _reason(o["reason"])
    reject = _GENERIC_REJECT_REASONS | {
        "STALE_BASIS",
        "AUDIENCE_DENIED",
        "CAPABILITY_UNAVAILABLE",
        "REGION_DENIED",
        "UNSAFE_TARGET",
        "BODY_BUSY",
        "QUARANTINED",
    }
    if (
        o["decision"] not in {"ACCEPTED", "REJECTED"}
        or (o["decision"] == "ACCEPTED" and o["reason"] is not None)
        or (o["decision"] == "REJECTED" and o["reason"] not in reject)
    ):
        _semantic("invalid admission reason")
    return o


def _cancel(v: Any) -> dict[str, Any]:
    o = _obj(v, {"mode", "targetRequestId", "targetEpoch", "deadline"})
    _i63(o["targetEpoch"])
    _utc(o["deadline"])
    _target_mode(o["mode"], o["targetRequestId"])
    return o


def _cancel_result(v: Any) -> dict[str, Any]:
    o = _obj(v, {"cancelId", "mode", "targetRequestId", "targetEpoch", "result", "reason", "stamp"})
    _uuid(o["cancelId"])
    _target_mode(o["mode"], o["targetRequestId"])
    _i63(o["targetEpoch"])
    _stamp(o["stamp"])
    _reason(o["reason"])
    allowed = {
        "TARGET": {"REQUESTED", "ALREADY_TERMINAL", "NOT_FOUND", "REJECTED", "UNKNOWN"},
        "EMERGENCY_REVOKE": {"REVOKED", "REJECTED", "UNKNOWN"},
    }
    reasons = {
        "REQUESTED": {None},
        "REVOKED": {None},
        "ALREADY_TERMINAL": {"TARGET_TERMINAL"},
        "NOT_FOUND": {"TARGET_NOT_FOUND"},
        "UNKNOWN": {"OUTCOME_UNPROVEN", "HISTORY_UNAVAILABLE"},
        "REJECTED": _GENERIC_REJECT_REASONS,
    }
    if o["result"] not in allowed.get(o["mode"], set()) or o["reason"] not in reasons[o["result"]]:
        _semantic("invalid cancel result")
    return o


def _receipt(v: Any) -> dict[str, Any]:
    o = _obj(v, {"requestId", "status", "reason", "stamp", "outcome"})
    _uuid(o["requestId"])
    _stamp(o["stamp"])
    _reason(o["reason"])
    outcome = _outcome(o["outcome"])
    status = o["status"]
    required = {
        "FAILED": {"UNSAFE_TARGET", "STUCK", "EXECUTION_REJECTED"},
        "TIMED_OUT": {"DEADLINE_EXPIRED"},
        "CANCELLED": {"CANCEL_CONFIRMED"},
        "DISCONNECTED": {"CONTROL_LOST"},
        "UNKNOWN": {"OUTCOME_UNPROVEN", "HISTORY_UNAVAILABLE"},
    }
    if status == "SUCCEEDED":
        if o["reason"] is not None or outcome["effect"] != "COMPLETE" or outcome["kind"] in {"UNDETERMINED", "STOPPED"}:
            _semantic("invalid succeeded receipt")
    elif status not in required or o["reason"] not in required[status]:
        _semantic("invalid receipt reason")
    if status == "UNKNOWN" and (outcome["kind"], outcome["effect"]) != ("UNDETERMINED", "UNKNOWN"):
        _semantic("invalid unknown outcome")
    if status != "SUCCEEDED" and status != "UNKNOWN" and outcome["effect"] not in {"NONE", "PARTIAL"}:
        _semantic("invalid non-success effect")
    if status in {"SUCCEEDED", "CANCELLED", "TIMED_OUT", "DISCONNECTED"} and not o["stamp"]["evidence"]:
        _semantic("receipt requires evidence")
    return o


def _reconcile(v: Any) -> dict[str, Any]:
    o = _obj(v, {"targetRequestId", "targetEpoch", "deadline"})
    _optional_uuid(o["targetRequestId"])
    _i63(o["targetEpoch"])
    _utc(o["deadline"])
    return o


def _reconcile_result(v: Any) -> dict[str, Any]:
    o = _obj(
        v,
        {
            "reconcileId",
            "targetRequestId",
            "targetEpoch",
            "result",
            "reason",
            "quiescent",
            "observationId",
            "admissionId",
            "receiptId",
            "stamp",
        },
    )
    _uuid(o["reconcileId"])
    _i63(o["targetEpoch"])
    _stamp(o["stamp"])
    _reason(o["reason"])
    r = o["result"]
    target = o["targetRequestId"]
    a = o["admissionId"]
    receipt = o["receiptId"]
    obs = o["observationId"]
    for x in (target, a, receipt, obs):
        _optional_uuid(x)
    rules = {
        "ADMISSION_REJECTED": (True, True, False, False, False, None),
        "IN_FLIGHT": (True, True, False, False, False, None),
        "KNOWN": (True, True, True, False, False, None),
        "QUIESCENT": (False, False, False, True, True, None),
        "BLOCKED": (False, False, False, False, False, "QUIESCENCE_UNPROVEN"),
        "UNKNOWN": (True, False, False, False, False, {"OUTCOME_UNPROVEN", "HISTORY_UNAVAILABLE"}),
        "REJECTED": (None, False, False, False, False, _GENERIC_REJECT_REASONS),
    }
    if r not in rules or type(o["quiescent"]) is not bool:
        _semantic("invalid reconcile result")
    need = rules[r]
    checks = (target is not None, a is not None, receipt is not None, obs is not None, o["quiescent"])
    if any(expected is not None and actual != expected for actual, expected in zip(checks, need[:5])):
        _semantic("invalid reconcile matrix")
    reason = need[5]
    if isinstance(reason, frozenset) and o["reason"] not in reason:
        _semantic(f"invalid reconcile reason result={r} reason={o['reason']}")
    elif isinstance(reason, frozenset):
        pass
    elif r == "UNKNOWN" and o["reason"] not in {"OUTCOME_UNPROVEN", "HISTORY_UNAVAILABLE"}:
        _semantic(f"invalid reconcile reason result={r} reason={o['reason']}")
    elif r == "UNKNOWN":
        pass
    elif reason is not None and o["reason"] != reason:
        _semantic(f"invalid reconcile reason result={r} reason={o['reason']}")
    elif reason is None and o["reason"] is not None:
        _semantic("invalid reconcile reason")
    return o


def _outcome(v: Any) -> dict[str, Any]:
    o = _obj(v, {"kind", "effect", "state", "observationId"})
    if o["kind"] not in {"SUMMARY", "PUBLIC_BROADCAST", "ORIENTATION", "POSITION", "STOPPED", "UNDETERMINED"} or o[
        "effect"
    ] not in {"NONE", "PARTIAL", "COMPLETE", "UNKNOWN"}:
        _semantic("invalid outcome")
    if o["state"] is not None:
        _state(o["state"])
    _optional_uuid(o["observationId"])
    if o["kind"] not in {"UNDETERMINED", "PUBLIC_BROADCAST"} and (o["state"] is None or o["observationId"] is None):
        _semantic("outcome requires state and observation")
    return o


def _stamp(v: Any) -> None:
    o = _obj(v, {"serverBootId", "serverRevision", "serverTick", "eventId", "evidence"})
    _uuid(o["serverBootId"])
    _uuid(o["eventId"])
    _i63(o["serverRevision"])
    _i63(o["serverTick"])
    if not isinstance(o["evidence"], list) or len(o["evidence"]) > 8 or len(set(o["evidence"])) != len(o["evidence"]):
        _semantic("invalid evidence")
    for x in o["evidence"]:
        _uuid(x)


def _basis(v: Any) -> None:
    o = _obj(v, {"observationId", "serverBootId", "serverRevision"})
    _uuid(o["observationId"])
    _uuid(o["serverBootId"])
    _i63(o["serverRevision"])


def _state(v: Any) -> None:
    o = _obj(v, {"dimensionId", "position", "yaw", "pitch"})
    _dimension(o["dimensionId"])
    _position(o["position"])
    yaw = _f64(o["yaw"])
    pitch = _f64(o["pitch"])
    if not -180 <= yaw < 180 or not -90 <= pitch <= 90:
        _semantic("invalid orientation")
    o["yaw"], o["pitch"] = yaw, pitch


def _position(v: Any) -> None:
    o = _obj(v, {"x", "y", "z"})
    values = {key: _f64(item) for key, item in o.items()}
    if any(abs(item) > 30000000 for item in values.values()):
        _semantic("position out of bounds")
    o.update(values)


def _obj(v: Any, keys: set[str]) -> dict[str, Any]:
    if not isinstance(v, dict) or set(v) != keys:
        _structure("object keys do not match")
    return v


def _id(v: Any) -> None:
    if not isinstance(v, str) or not _IDS.fullmatch(v):
        _semantic("invalid Id")


def _dimension(v: Any) -> None:
    if not isinstance(v, str) or not re.fullmatch(r"[a-z0-9_.-]+:[a-z0-9/._-]+", v):
        _semantic("invalid dimension")


def _uuid(v: Any) -> None:
    if not isinstance(v, str) or not _UUID.fullmatch(v):
        _semantic("invalid UUID")


def _optional_uuid(v: Any) -> None:
    if v is not None:
        _uuid(v)


def _i63(v: Any) -> None:
    if isinstance(v, _JsonNumber):
        raise MinecraftWireError(WireErrorCategory.LEXICAL, "I63 must use integer JSON syntax")
    if type(v) is not int or not 0 <= v <= _I63_MAX:
        _semantic("invalid I63")


def _f64(v: Any) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float, Decimal)):
        _semantic("invalid F64")
    try:
        result = float(v)
    except (ValueError, OverflowError):
        _semantic("invalid F64")
    if not math.isfinite(result):
        _semantic("invalid F64")
    return 0.0 if not result else result


def _utc(v: Any) -> datetime:
    if not isinstance(v, str):
        _semantic("invalid UTC")
    match = _UTC.fullmatch(v)
    if match is None:
        _semantic("invalid UTC")
    try:
        year, month, day, hour, minute, second, milliseconds = map(int, match.groups())
        return datetime(year, month, day, hour, minute, second, milliseconds * 1000, tzinfo=timezone.utc)
    except ValueError as error:
        raise MinecraftWireError(WireErrorCategory.SEMANTIC, "invalid UTC calendar") from error


def _format_utc(v: datetime) -> str:
    if not isinstance(v, datetime) or v.tzinfo is None or v.utcoffset() is None or v.microsecond % 1000:
        _semantic("timestamp must be aligned UTC")
    v = v.astimezone(timezone.utc)
    return (
        f"{v.year:04d}-{v.month:02d}-{v.day:02d}T{v.hour:02d}:{v.minute:02d}:{v.second:02d}.{v.microsecond//1000:03d}Z"
    )


def _text(v: Any, scalars: int, byte_limit: int, nul: bool) -> None:
    if (
        not isinstance(v, str)
        or not v
        or len(v) > scalars
        or len(v.encode("utf-8")) > byte_limit
        or (nul and "\0" in v)
    ):
        _semantic("invalid text")


def _target_mode(mode: Any, target: Any) -> None:
    if mode == "TARGET" and target is not None:
        _uuid(target)
    elif mode == "EMERGENCY_REVOKE" and target is None:
        return
    else:
        _semantic("invalid cancel target")


def _reason(v: Any) -> None:
    if v is not None and v not in _REASONS:
        _semantic("invalid reason")


def _has_surrogate(v: str) -> bool:
    return any(0xD800 <= ord(c) <= 0xDFFF for c in v)


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _json_numbers(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_numbers(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_numbers(item) for item in value]
    if type(value) is float and value == 0:
        return 0
    return value


def _contains_surrogate(v: Any) -> bool:
    if isinstance(v, str):
        return _has_surrogate(v)
    if isinstance(v, dict):
        return any(_contains_surrogate(k) or _contains_surrogate(x) for k, x in v.items())
    if isinstance(v, list):
        return any(_contains_surrogate(x) for x in v)
    return False


def _structure(message: str) -> None:
    raise MinecraftWireError(WireErrorCategory.STRUCTURE, message)


def _semantic(message: str) -> None:
    raise MinecraftWireError(WireErrorCategory.SEMANTIC, message)
