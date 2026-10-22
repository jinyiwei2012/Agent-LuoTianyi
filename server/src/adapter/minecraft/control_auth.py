"""Offline control-auth transcript and one-time pending handshake primitives.

This module deliberately owns neither transport routing nor credential, epoch, body,
or permit state.  Its callers supply allowlisted, trusted connection context.
"""

from __future__ import annotations

import base64
import hmac
import math
import re
import secrets
import struct
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from time import monotonic
from uuid import uuid4

_DOMAIN = b"luosandbox.control-auth.v1"
_HUB_ROLE = "AGENT_HUB"
_ROLES = frozenset(("SERVER_AUTHORITY", "CLIENT_CONTROLLER", _HUB_ROLE))
_LABELS = frozenset(("HUB_PROOF", "PEER_PROOF", "READY_PROOF"))
_B32_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_I63_MAX = (1 << 63) - 1
_CHALLENGE_LIFETIME = timedelta(seconds=5)
_MAX_CLOCK_SKEW = timedelta(seconds=1)


class ControlAuthError(ValueError):
    """Raised when an offline authentication value violates the wire contract."""


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class ControlAuthContext:
    """Allowlisted identity and deployment hashes, never payload-derived at consume time."""

    peer_role: str
    peer_install_id: str
    hub_install_id: str
    key_id: str
    protocol: str
    mod_hash: bytes
    config_hash: bytes
    binding_hash: bytes

    def __post_init__(self) -> None:
        if self.peer_role not in _ROLES or any(
            not _ascii_id(value) for value in (self.peer_install_id, self.hub_install_id, self.key_id, self.protocol)
        ):
            raise ControlAuthError("invalid auth context")
        object.__setattr__(self, "mod_hash", _bytes32(self.mod_hash, "hash"))
        object.__setattr__(self, "config_hash", _bytes32(self.config_hash, "hash"))
        object.__setattr__(self, "binding_hash", _bytes32(self.binding_hash, "hash"))


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class PendingChallenge:
    """Internal one-use state for exactly one trusted transport connection."""

    connection_id: str
    challenge_id: str
    context: ControlAuthContext
    peer_nonce: bytes
    hub_nonce: bytes
    issued_at: datetime
    expires_at: datetime
    issued_monotonic: float
    deadline: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "peer_nonce", _bytes32(self.peer_nonce, "nonce"))
        object.__setattr__(self, "hub_nonce", _bytes32(self.hub_nonce, "nonce"))


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class AuthenticatedHandshake:
    """Immutable result of consuming a valid pending challenge; it contains no key."""

    context: ControlAuthContext
    challenge_id: str
    ready_proof: str


# Keep the initial narrow name available to callers that already imported it.
AuthenticatedControlContext = AuthenticatedHandshake


def b32_encode(value: bytes) -> str:
    """Encode precisely 32 raw bytes as canonical unpadded base64url."""
    return base64.urlsafe_b64encode(_bytes32(value, "B32 bytes")).decode("ascii").rstrip("=")


def b32_decode(value: str) -> bytes:
    """Decode a canonical 43-character base64url representation of 32 bytes."""
    if type(value) is not str or len(value) != 43 or any(character not in _B32_ALPHABET for character in value):
        raise ControlAuthError("invalid B32")
    try:
        decoded = base64.urlsafe_b64decode(value + "=")
    except (ValueError, UnicodeError) as error:
        raise ControlAuthError("invalid B32") from error
    decoded = _bytes32(decoded, "B32")
    if b32_encode(decoded) != value:
        raise ControlAuthError("noncanonical B32")
    return decoded


def transcript(
    label: str,
    context: ControlAuthContext,
    peer_nonce: bytes,
    hub_nonce: bytes,
    challenge_id: str,
    issued_at: datetime,
    expires_at: datetime,
) -> bytes:
    """Build the specified 16-field U32BE length-prefixed HMAC transcript."""
    if type(label) is not str or label not in _LABELS or not isinstance(context, ControlAuthContext):
        raise ControlAuthError("invalid transcript")
    if not _uuid(challenge_id):
        raise ControlAuthError("invalid challenge id")
    fields = (
        _DOMAIN,
        label.encode("ascii"),
        context.peer_role.encode("ascii"),
        _HUB_ROLE.encode("ascii"),
        context.peer_install_id.encode("ascii"),
        context.hub_install_id.encode("ascii"),
        context.key_id.encode("ascii"),
        context.protocol.encode("ascii"),
        context.mod_hash,
        _bytes32(peer_nonce, "nonce"),
        _bytes32(hub_nonce, "nonce"),
        challenge_id.encode("ascii"),
        _utc(issued_at).encode("ascii"),
        _utc(expires_at).encode("ascii"),
        context.config_hash,
        context.binding_hash,
    )
    return b"".join(struct.pack(">I", len(field)) + field for field in fields)


def compute_proof(
    key: bytes,
    label: str,
    context: ControlAuthContext,
    peer_nonce: bytes,
    hub_nonce: bytes,
    challenge_id: str,
    issued_at: datetime,
    expires_at: datetime,
) -> str:
    """Return the direction-separated HMAC proof in canonical B32 form."""
    digest = hmac.new(
        _bytes32(key, "key"),
        transcript(label, context, peer_nonce, hub_nonce, challenge_id, issued_at, expires_at),
        sha256,
    ).digest()
    return b32_encode(digest)


def verify_proof(
    proof: str,
    key: bytes,
    label: str,
    context: ControlAuthContext,
    peer_nonce: bytes,
    hub_nonce: bytes,
    challenge_id: str,
    issued_at: datetime,
    expires_at: datetime,
) -> bool:
    """Strictly decode then compare proof bytes with ``hmac.compare_digest``."""
    try:
        supplied = b32_decode(proof)
        expected = b32_decode(
            compute_proof(key, label, context, peer_nonce, hub_nonce, challenge_id, issued_at, expires_at)
        )
    except ControlAuthError:
        return False
    return hmac.compare_digest(supplied, expected)


class PendingHandshakes:
    """Lock-protected global-four, per-connection-one, single-use challenges."""

    def __init__(self, *, maximum: int = 4, ttl_seconds: float = 5.0):
        if type(maximum) is not int or not 1 <= maximum <= 4:
            raise ControlAuthError("invalid pending limits")
        if type(ttl_seconds) not in (int, float) or isinstance(ttl_seconds, bool) or ttl_seconds != 5:
            raise ControlAuthError("pending lifetime must be five seconds")
        self._maximum = maximum
        self._items: dict[str, PendingChallenge] = {}
        self._lock = threading.Lock()

    def begin(
        self,
        connection_id: str,
        context: ControlAuthContext,
        peer_nonce: bytes,
        issued_at: datetime,
        *,
        hub_nonce: bytes | None = None,
        challenge_id: str | None = None,
        now: float | None = None,
        utc_now: datetime,
    ) -> PendingChallenge:
        """Atomically allocate one challenge, refusing capacity rather than evicting it."""
        if not _ascii_id(connection_id) or not isinstance(context, ControlAuthContext):
            raise ControlAuthError("invalid hello")
        peer_nonce = _bytes32(peer_nonce, "nonce")
        issued = _utc_datetime(issued_at)
        current = _monotonic(now)
        _validate_issued_clock(issued, _trusted_utc_now(utc_now))
        nonce = secrets.token_bytes(32) if hub_nonce is None else _bytes32(hub_nonce, "nonce")
        cid = str(uuid4()) if challenge_id is None else challenge_id
        if not _uuid(cid):
            raise ControlAuthError("invalid challenge")
        pending = PendingChallenge(
            connection_id=connection_id,
            challenge_id=cid,
            context=context,
            peer_nonce=peer_nonce,
            hub_nonce=nonce,
            issued_at=issued,
            expires_at=issued + _CHALLENGE_LIFETIME,
            issued_monotonic=current,
            deadline=current + _CHALLENGE_LIFETIME.total_seconds(),
        )
        with self._lock:
            self._prune_locked(current)
            if connection_id in self._items or len(self._items) >= self._maximum:
                raise ControlAuthError("pending capacity")
            self._items[connection_id] = pending
        return pending

    def consume(
        self,
        connection_id: str,
        challenge_id: str,
        peer_proof: str,
        key: bytes,
        *,
        expected_context: ControlAuthContext | None = None,
        now: float | None = None,
        utc_now: datetime,
    ) -> AuthenticatedHandshake:
        """Atomically remove and verify a challenge; every attempted proof consumes it."""
        current = _monotonic(now)
        if not _ascii_id(connection_id) or not _uuid(challenge_id):
            raise ControlAuthError("proof rejected")
        trusted_utc_now = _trusted_utc_now(utc_now)
        with self._lock:
            self._prune_locked(current)
            pending = self._items.pop(connection_id, None)
            if (
                pending is None
                or pending.challenge_id != challenge_id
                or current < pending.issued_monotonic
                or current >= pending.deadline
                or (
                    expected_context is not None
                    and (not isinstance(expected_context, ControlAuthContext) or expected_context != pending.context)
                )
                or not _valid_at_wall_clock(pending, trusted_utc_now)
                or not verify_proof(
                    peer_proof,
                    key,
                    "PEER_PROOF",
                    pending.context,
                    pending.peer_nonce,
                    pending.hub_nonce,
                    pending.challenge_id,
                    pending.issued_at,
                    pending.expires_at,
                )
            ):
                raise ControlAuthError("proof rejected")
            ready = compute_proof(
                key,
                "READY_PROOF",
                pending.context,
                pending.peer_nonce,
                pending.hub_nonce,
                pending.challenge_id,
                pending.issued_at,
                pending.expires_at,
            )
            return AuthenticatedHandshake(context=pending.context, challenge_id=pending.challenge_id, ready_proof=ready)

    def discard(self, connection_id: str) -> bool:
        """Remove only this connection's pending challenge; repeated calls are safe."""
        if not _ascii_id(connection_id):
            raise ControlAuthError("invalid connection id")
        with self._lock:
            return self._items.pop(connection_id, None) is not None

    def _prune_locked(self, now: float) -> None:
        for connection_id, pending in tuple(self._items.items()):
            if now >= pending.deadline:
                self._items.pop(connection_id, None)


def _ascii_id(value: object) -> bool:
    return type(value) is str and 0 < len(value) <= 128 and value.isascii() and "\0" not in value


def _bytes32(value: object, description: str) -> bytes:
    if not isinstance(value, bytes) or len(value) != 32:
        raise ControlAuthError(f"invalid {description}")
    return bytes(value)


def _uuid(value: object) -> bool:
    return type(value) is str and _UUID_RE.fullmatch(value) is not None


def _monotonic(value: float | None) -> float:
    result = monotonic() if value is None else value
    if type(result) not in (int, float) or isinstance(result, bool) or not math.isfinite(result) or result < 0:
        raise ControlAuthError("invalid monotonic clock")
    return float(result)


def _utc_datetime(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
        or value.microsecond % 1000 != 0
        or not 1 <= value.year <= 9999
    ):
        raise ControlAuthError("invalid UTC")
    _to_ms(value)
    return value.replace(tzinfo=timezone.utc)


def _trusted_utc_now(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ControlAuthError("invalid trusted UTC clock")
    _to_ms(value)
    return value.replace(tzinfo=timezone.utc)


def _utc(value: datetime) -> str:
    normalized = _utc_datetime(value)
    return (
        f"{normalized.year:04d}-{normalized.month:02d}-{normalized.day:02d}T"
        f"{normalized.hour:02d}:{normalized.minute:02d}:{normalized.second:02d}."
        f"{normalized.microsecond // 1000:03d}Z"
    )


def _to_ms(value: datetime) -> int:
    normalized = value.replace(tzinfo=timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = normalized - epoch
    milliseconds = delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000
    if type(milliseconds) is not int or not -_I63_MAX <= milliseconds <= _I63_MAX:
        raise ControlAuthError("UTC milliseconds outside I63")
    return milliseconds


def _validate_issued_clock(issued_at: datetime, utc_now: datetime) -> None:
    if issued_at > utc_now + _MAX_CLOCK_SKEW or issued_at + _CHALLENGE_LIFETIME <= utc_now:
        raise ControlAuthError("invalid UTC challenge time")


def _valid_at_wall_clock(pending: PendingChallenge, utc_now: datetime) -> bool:
    return pending.issued_at <= utc_now + _MAX_CLOCK_SKEW and utc_now < pending.expires_at
