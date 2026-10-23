"""Inactive v2 peer provisioning from verified deployment evidence.

This module binds caller-supplied role key material to an already verified
deployment snapshot.  It does not obtain credentials, authenticate a transport,
consume a pending handshake, activate control, or grant a runtime role.
"""

from __future__ import annotations

import hmac
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .control_auth import ControlAuthContext, ControlAuthError, compute_proof, verify_proof
from .deployment_files import VerifiedDeploymentFilesystem
from .postauth_keys import AuthenticatedTranscript, PostAuthKeyError, PostAuthSessionKeys

_PROTOCOL = "LUO_CONTROL_POSTAUTH_V2"
_ROLES = frozenset(("SERVER_AUTHORITY", "CLIENT_CONTROLLER"))
_ASCII_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_ZERO_UUID = "00000000-0000-0000-0000-000000000000"
_MINTED = object()


class V2StartupError(ValueError):
    """Raised when inactive v2 provisioning or a fixed crypto operation fails."""


@dataclass(frozen=True, slots=True, repr=False)
class ReadyProof:
    """A READY_PROOF wire value; this is not authentication or runtime authority."""

    proof: str

    def __post_init__(self) -> None:
        if type(self.proof) is not str:
            raise V2StartupError("invalid ready proof")

    def __repr__(self) -> str:
        return "ReadyProof(<redacted>)"


class _RolePskOwner:
    __slots__ = ("_key", "_lock", "_closed")

    def __init__(self, role_psk: bytes):
        self._key = bytearray(role_psk)
        self._lock = threading.RLock()
        self._closed = False

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def compute(self, label: str, context: ControlAuthContext, values: tuple) -> str:
        with self._lock:
            key = self._ephemeral_key_locked()
            try:
                return compute_proof(key, label, context, *values)
            except ControlAuthError as error:
                raise V2StartupError("v2 proof computation rejected") from error
            finally:
                key = b""

    def verify(self, proof: str, label: str, context: ControlAuthContext, values: tuple) -> bool:
        with self._lock:
            key = self._ephemeral_key_locked()
            try:
                return verify_proof(proof, key, label, context, *values)
            except ControlAuthError as error:
                raise V2StartupError("v2 proof verification rejected") from error
            finally:
                key = b""

    def derive(self, authenticated: AuthenticatedTranscript) -> PostAuthSessionKeys:
        with self._lock:
            key = self._ephemeral_key_locked()
            try:
                return PostAuthSessionKeys(authenticated, key)
            finally:
                key = b""

    def _ephemeral_key_locked(self) -> bytes:
        if self._closed:
            raise V2StartupError("v2 credential owner is closed")
        return bytes(self._key)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                for index in range(len(self._key)):
                    self._key[index] = 0
            finally:
                self._closed = True

    def __copy__(self):
        raise V2StartupError("v2 credential owner cannot be copied")

    def __deepcopy__(self, memo):
        raise V2StartupError("v2 credential owner cannot be copied")

    def __reduce_ex__(self, protocol):
        raise V2StartupError("v2 credential owner cannot be serialized")


@dataclass(frozen=True, slots=True, init=False, repr=False)
class ProvisionedV2Peer:
    """Privately minted inactive context plus an owned, closable role PSK."""

    context: ControlAuthContext
    _owner: _RolePskOwner
    _marker: object

    def __init__(self, *args, **kwargs):
        raise V2StartupError("v2 peer is privately provisioned")

    @classmethod
    def _mint(cls, context: ControlAuthContext, owner: _RolePskOwner):
        value = object.__new__(cls)
        object.__setattr__(value, "context", context)
        object.__setattr__(value, "_owner", owner)
        object.__setattr__(value, "_marker", _MINTED)
        return value

    @property
    def is_provisioned(self) -> bool:
        """Whether private provenance exists and its credential remains active."""
        return self._marker is _MINTED and not self._owner.closed

    def compute_hub_proof(
        self,
        peer_nonce: bytes,
        hub_nonce: bytes,
        challenge_id: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> str:
        values = _strict_transcript_values(peer_nonce, hub_nonce, challenge_id, issued_at, expires_at)
        return self._owner.compute("HUB_PROOF", self.context, values)

    def verify_peer_proof_and_ready(
        self,
        peer_proof: str,
        peer_nonce: bytes,
        hub_nonce: bytes,
        challenge_id: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> ReadyProof:
        values = _strict_transcript_values(peer_nonce, hub_nonce, challenge_id, issued_at, expires_at)
        if not self._owner.verify(peer_proof, "PEER_PROOF", self.context, values):
            raise V2StartupError("peer proof rejected")
        return ReadyProof(self._owner.compute("READY_PROOF", self.context, values))

    def derive_session_keys(self, authenticated: AuthenticatedTranscript) -> PostAuthSessionKeys:
        """Derive keys from caller-trusted transcript data, not handshake provenance."""
        _strict_authenticated_transcript(authenticated, self.context)
        try:
            return self._owner.derive(authenticated)
        except PostAuthKeyError as error:
            raise V2StartupError("session key derivation rejected") from error

    def close(self) -> None:
        self._owner.close()

    def __enter__(self):
        if not self.is_provisioned:
            raise V2StartupError("v2 peer is closed")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def __copy__(self):
        raise V2StartupError("v2 peer cannot be copied")

    def __deepcopy__(self, memo):
        raise V2StartupError("v2 peer cannot be copied")

    def __reduce_ex__(self, protocol):
        raise V2StartupError("v2 peer cannot be serialized")

    def __repr__(self) -> str:
        return "ProvisionedV2Peer(<redacted>)"


def provision_v2_peer(
    filesystem: VerifiedDeploymentFilesystem,
    *,
    expected_peer_role: str,
    expected_peer_install_id: str,
    expected_hub_install_id: str,
    expected_config_hash: bytes,
    key_id: str,
    role_psk: bytes,
) -> ProvisionedV2Peer:
    """Bind external pre-provisioned key material to verified snapshot identity."""
    try:
        verified = type(filesystem) is VerifiedDeploymentFilesystem and filesystem.filesystem_verified
    except (AttributeError, TypeError):
        verified = False
    if not verified:
        raise V2StartupError("verified deployment filesystem required")
    if (
        type(expected_peer_role) is not str
        or expected_peer_role not in _ROLES
        or not all(_ascii_id(value) for value in (expected_peer_install_id, expected_hub_install_id, key_id))
    ):
        raise V2StartupError("invalid expected v2 identity")
    if type(expected_config_hash) is not bytes or len(expected_config_hash) != 32:
        raise V2StartupError("invalid expected config hash")
    if type(role_psk) is not bytes or len(role_psk) != 32:
        raise V2StartupError("invalid role PSK")
    snapshot = filesystem.snapshot
    if (
        snapshot.peer_role != expected_peer_role
        or snapshot.peer_install_id != expected_peer_install_id
        or snapshot.hub_install_id != expected_hub_install_id
        or not hmac.compare_digest(snapshot.config_hash, expected_config_hash)
    ):
        raise V2StartupError("verified deployment identity mismatch")
    try:
        context = ControlAuthContext(
            peer_role=snapshot.peer_role,
            peer_install_id=snapshot.peer_install_id,
            hub_install_id=snapshot.hub_install_id,
            key_id=key_id,
            protocol=_PROTOCOL,
            mod_hash=snapshot.mod_manifest_hash,
            config_hash=snapshot.config_hash,
            binding_hash=snapshot.binding_manifest_hash,
        )
    except ControlAuthError as error:
        raise V2StartupError("invalid verified v2 context") from error
    return ProvisionedV2Peer._mint(context, _RolePskOwner(role_psk))


def _strict_authenticated_transcript(authenticated: object, context: ControlAuthContext) -> None:
    if type(authenticated) is not AuthenticatedTranscript or authenticated.context != context:
        raise V2StartupError("complete matching authenticated transcript required")
    _strict_transcript_values(
        authenticated.peer_nonce,
        authenticated.hub_nonce,
        authenticated.challenge_id,
        authenticated.issued_at,
        authenticated.expires_at,
    )


def _strict_transcript_values(peer_nonce, hub_nonce, challenge_id, issued_at, expires_at) -> tuple:
    if (
        type(peer_nonce) is not bytes
        or len(peer_nonce) != 32
        or not any(peer_nonce)
        or type(hub_nonce) is not bytes
        or len(hub_nonce) != 32
        or not any(hub_nonce)
        or type(challenge_id) is not str
        or _UUID.fullmatch(challenge_id) is None
        or challenge_id == _ZERO_UUID
    ):
        raise V2StartupError("invalid v2 transcript identity")
    issued = _strict_utc(issued_at)
    expires = _strict_utc(expires_at)
    if expires - issued != timedelta(seconds=5):
        raise V2StartupError("invalid v2 transcript lifetime")
    return peer_nonce, hub_nonce, challenge_id, issued, expires


def _strict_utc(value: object) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
        or value.microsecond % 1000 != 0
        or not 1 <= value.year <= 9999
    ):
        raise V2StartupError("invalid canonical UTC timestamp")
    return value.replace(tzinfo=timezone.utc)


def _ascii_id(value: object) -> bool:
    return type(value) is str and _ASCII_ID.fullmatch(value) is not None
