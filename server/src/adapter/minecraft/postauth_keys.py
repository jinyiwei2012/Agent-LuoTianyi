"""Unassembled v2 post-auth directional key derivation."""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from .control_auth import ControlAuthContext, ControlAuthError, transcript

_V2_PROTOCOL = "LUO_CONTROL_POSTAUTH_V2"
_ROLES = frozenset(("SERVER_AUTHORITY", "CLIENT_CONTROLLER"))
_DIRECTIONS = frozenset(("PEER_TO_HUB", "HUB_TO_PEER"))
_ASCII_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_ZERO_UUID = "00000000-0000-0000-0000-000000000000"


class PostAuthKeyError(ValueError):
    """Raised when v2 key material cannot be safely derived or used."""


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class AuthenticatedTranscript:
    """Trusted, already-authenticated inputs for one v2 connection."""

    context: ControlAuthContext
    peer_nonce: bytes
    hub_nonce: bytes
    challenge_id: str
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.context, ControlAuthContext) or self.context.protocol != _V2_PROTOCOL:
            raise PostAuthKeyError("v2 trusted context required")
        if self.context.peer_role not in _ROLES or not all(
            _ascii_id(value)
            for value in (
                self.context.peer_install_id,
                self.context.hub_install_id,
                self.context.key_id,
                self.context.protocol,
            )
        ):
            raise PostAuthKeyError("invalid trusted peer role")
        if not _uuid(self.challenge_id):
            raise PostAuthKeyError("invalid trusted challenge id")


class PostAuthSessionKeys:
    """Private directional keys; this object is not a session or capability."""

    __slots__ = ("_keys", "_binding", "_closed")

    def __init__(self, authenticated: AuthenticatedTranscript, role_psk: bytes):
        if not isinstance(authenticated, AuthenticatedTranscript) or type(role_psk) is not bytes or len(role_psk) != 32:
            raise PostAuthKeyError("invalid key derivation input")
        try:
            digest = _authenticated_digest(authenticated)
        except ControlAuthError as error:
            raise PostAuthKeyError("invalid authenticated transcript") from error
        psk = bytearray(role_psk)
        try:
            self._keys = {
                direction: bytearray(
                    hmac.new(psk, _lp(b"LUO_CONTROL_SESSION_KEY_V2", digest, direction.encode()), sha256).digest()
                )
                for direction in _DIRECTIONS
            }
        finally:
            for index in range(len(psk)):
                psk[index] = 0
        self._closed = False
        self._binding = digest

    def require_context(self, authenticated: AuthenticatedTranscript) -> None:
        """Reject a context other than the complete authenticated transcript."""
        if self._closed or not isinstance(authenticated, AuthenticatedTranscript):
            raise PostAuthKeyError("closed or invalid authenticated context")
        try:
            candidate = _authenticated_digest(authenticated)
        except PostAuthKeyError:
            raise
        if not hmac.compare_digest(self._binding, candidate):
            raise PostAuthKeyError("authenticated context mismatch")

    def mac(self, direction: str, message: bytes) -> bytes:
        if self._closed or direction not in _DIRECTIONS or type(message) is not bytes:
            raise PostAuthKeyError("closed or invalid directional key")
        return hmac.new(self._keys[direction], message, sha256).digest()

    def verify(self, direction: str, message: bytes, supplied: bytes) -> bool:
        if self._closed or type(supplied) is not bytes or len(supplied) != 32:
            return False
        try:
            return hmac.compare_digest(self.mac(direction, message), supplied)
        except PostAuthKeyError:
            return False

    def close(self) -> None:
        if not self._closed:
            for key in self._keys.values():
                for index in range(len(key)):
                    key[index] = 0
            self._binding = b"\0" * len(self._binding)
            self._closed = True


def _lp(*fields: bytes) -> bytes:
    import struct

    return b"".join(struct.pack(">I", len(field)) + field for field in fields)


def _authenticated_digest(authenticated: AuthenticatedTranscript) -> bytes:
    try:
        return sha256(
            transcript(
                "PEER_PROOF",
                authenticated.context,
                authenticated.peer_nonce,
                authenticated.hub_nonce,
                authenticated.challenge_id,
                authenticated.issued_at,
                authenticated.expires_at,
            )
        ).digest()
    except ControlAuthError as error:
        raise PostAuthKeyError("invalid authenticated transcript") from error


def _ascii_id(value: object) -> bool:
    return type(value) is str and _ASCII_ID.fullmatch(value) is not None


def _uuid(value: object) -> bool:
    return type(value) is str and _UUID.fullmatch(value) is not None and value != _ZERO_UUID
