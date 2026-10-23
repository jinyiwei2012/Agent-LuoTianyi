"""Disabled-actuation authenticated Minecraft WebSocket transport primitives."""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol
from uuid import uuid4

from src.adapter.minecraft.control_auth import (
    AuthenticatedHandshake,
    ControlAuthContext,
    ControlAuthError,
    PendingHandshakes,
    b32_decode,
    b32_encode,
    compute_proof,
)

_SECURITY = "luosandbox.control-auth.v1"
_SUBPROTOCOL = "luosandbox.minecraft.v1"
_ROLES = frozenset(("SERVER_AUTHORITY", "CLIENT_CONTROLLER"))
_MAX_FRAME_BYTES = 4096
_MAX_DEPTH = 4
_HEARTBEAT_SECONDS = 0.5
_CONTROL_LOST_SECONDS = 2.0
_CLOSE_TIMEOUT_SECONDS = 0.1


class TransportError(RuntimeError):
    """Fail-closed transport error containing no credential material."""


@dataclass(frozen=True, slots=True, repr=False)
class AllowedPeer:
    context: ControlAuthContext
    key: bytes

    def __post_init__(self) -> None:
        if self.context.peer_role not in _ROLES or not isinstance(self.key, bytes) or len(self.key) != 32:
            raise ValueError("invalid transport peer")


@dataclass(frozen=True, slots=True)
class AuthenticatedTransportConnection:
    connection_id: str
    handshake: AuthenticatedHandshake


class WebSocketPort(Protocol):
    @property
    def headers(self): ...

    @property
    def client(self): ...

    async def accept(self, *, subprotocol: str | None = None) -> None: ...

    async def receive_text(self) -> str: ...

    async def send_text(self, data: str) -> None: ...

    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


class MinecraftAuthenticatedTransport:
    """One-worker role authentication and disabled post-auth heartbeat loop."""

    def __init__(
        self,
        *,
        peers: tuple[AllowedPeer, ...],
        hub_install_id: str,
        monotonic_now: Callable[[], float],
        utc_now: Callable[[], datetime],
        nonce: Callable[[], bytes] | None = None,
        challenge_id: Callable[[], str] | None = None,
    ) -> None:
        if not peers or not callable(monotonic_now) or not callable(utc_now):
            raise ValueError("enabled transport requires provisioned peers and clocks")
        self._peers = {(item.context.peer_role, item.context.peer_install_id): item for item in peers}
        if len(self._peers) != len(peers):
            raise ValueError("duplicate transport peer")
        self._hub_install_id = hub_install_id
        self._monotonic_now = monotonic_now
        self._utc_now = utc_now
        self._nonce = nonce
        self._challenge_id = challenge_id
        self._pending = PendingHandshakes(maximum=4)
        self._roles: dict[str, tuple[str, str]] = {}
        self._unauthenticated: set[str] = set()
        self._last_monotonic: float | None = None
        self._clock_failed = False
        self._attempts: dict[str, _Bucket] = {key[1]: _Bucket(1.0, 2.0) for key in self._peers}
        self._global_attempts = _Bucket(2.0, 4.0)

    @property
    def active_roles(self) -> tuple[str, ...]:
        return tuple(sorted(role for role, (_connection, state) in self._roles.items() if state == "AUTHENTICATED"))

    @property
    def pending_count(self) -> int:
        return len(self._unauthenticated)

    async def handle(self, websocket: WebSocketPort) -> None:
        connection_id = str(uuid4())
        role = None
        try:
            self._reserve_unauthenticated(connection_id)
            loop_deadline = asyncio.get_running_loop().time() + 5.0
            await websocket.accept(subprotocol=_SUBPROTOCOL)
            hello = await self._receive(websocket, timeout=self._remaining(loop_deadline))
            peer = self._hello_peer(hello)
            role = peer.context.peer_role
            self._admit_attempt(peer.context.peer_install_id)
            if role in self._roles:
                raise TransportError("authenticated role already connected")
            self._roles[role] = (connection_id, "HANDSHAKING")
            issued = self._trusted_utc()
            peer_nonce = b32_decode(hello["peerNonce"])
            pending = self._pending.begin(
                connection_id,
                peer.context,
                peer_nonce,
                issued,
                hub_nonce=None if self._nonce is None else self._nonce(),
                challenge_id=None if self._challenge_id is None else self._challenge_id(),
                now=self._monotonic(),
                utc_now=issued,
            )
            await self._send(
                websocket,
                _dump(
                    {
                        "security": _SECURITY,
                        "kind": "CHALLENGE",
                        "challengeId": pending.challenge_id,
                        "hubNonce": b32_encode(pending.hub_nonce),
                        "issuedAt": _utc(pending.issued_at),
                        "expiresAt": _utc(pending.expires_at),
                        "hubProof": compute_proof(
                            peer.key,
                            "HUB_PROOF",
                            pending.context,
                            pending.peer_nonce,
                            pending.hub_nonce,
                            pending.challenge_id,
                            pending.issued_at,
                            pending.expires_at,
                        ),
                    }
                ),
                timeout=self._remaining(loop_deadline),
            )
            proof = await self._receive(
                websocket,
                timeout=min(self._remaining(loop_deadline), max(0.0, pending.deadline - self._monotonic())),
            )
            _exact(proof, {"security", "kind", "challengeId", "peerProof"}, "PROOF")
            handshake = self._pending.consume(
                connection_id,
                proof["challengeId"],
                proof["peerProof"],
                peer.key,
                expected_context=peer.context,
                now=self._monotonic(),
                utc_now=self._trusted_utc(),
            )
            await self._send(
                websocket,
                _dump(
                    {
                        "security": _SECURITY,
                        "kind": "READY",
                        "challengeId": handshake.challenge_id,
                        "readyProof": handshake.ready_proof,
                    }
                ),
                timeout=self._remaining(loop_deadline),
            )
            if self._roles.get(role) != (connection_id, "HANDSHAKING"):
                raise TransportError("role reservation lost")
            self._roles[role] = (connection_id, "AUTHENTICATED")
            self._unauthenticated.discard(connection_id)
            await self._heartbeat_loop(websocket)
        except asyncio.CancelledError as cancellation:
            self._release(connection_id, role)
            await _safe_close(websocket, 1001, "FEATURE_DISABLED")
            raise cancellation
        except (
            TransportError,
            ControlAuthError,
            TimeoutError,
            asyncio.TimeoutError,
            ValueError,
            TypeError,
            RuntimeError,
        ):
            self._release(connection_id, role)
            await _safe_close(websocket, 1008, "FEATURE_DISABLED")
        finally:
            self._release(connection_id, role)

    async def _heartbeat_loop(self, websocket: WebSocketPort) -> None:
        last_receive = self._monotonic()
        loop = asyncio.get_running_loop()
        loop_deadline = loop.time() + _CONTROL_LOST_SECONDS
        inbound_sequence = -1
        sender = asyncio.create_task(self._heartbeat_sender(websocket), name="minecraft-heartbeat-send")
        receive: asyncio.Task[dict] | None = None
        try:
            while True:
                remaining = min(
                    _CONTROL_LOST_SECONDS - (self._monotonic() - last_receive),
                    loop_deadline - loop.time(),
                )
                if remaining <= 0:
                    raise TransportError("heartbeat timeout")
                receive = asyncio.create_task(self._receive(websocket, timeout=remaining))
                done, _ = await asyncio.wait((receive, sender), return_when=asyncio.FIRST_COMPLETED)
                if sender in done:
                    receive.cancel()
                    await asyncio.gather(receive, return_exceptions=True)
                    receive = None
                    sender.result()
                    raise TransportError("heartbeat sender stopped")
                message = receive.result()
                receive = None
                _exact(message, {"security", "kind", "sequence"}, "HEARTBEAT")
                sequence = message["sequence"]
                if type(sequence) is not int or not 0 <= sequence <= (1 << 63) - 1 or sequence <= inbound_sequence:
                    raise TransportError("invalid heartbeat sequence")
                inbound_sequence = sequence
                last_receive = self._monotonic()
                loop_deadline = loop.time() + _CONTROL_LOST_SECONDS
        finally:
            children = tuple(task for task in (receive, sender) if task is not None)
            for task in children:
                task.cancel()
            cleanup = asyncio.create_task(self._drain_heartbeat_children(children), name="minecraft-heartbeat-cleanup")
            cancellation: asyncio.CancelledError | None = None
            while not cleanup.done():
                try:
                    await asyncio.sleep(0)
                except asyncio.CancelledError as error:
                    cancellation = cancellation or error
            cleanup.result()
            if cancellation is not None:
                raise cancellation

    @staticmethod
    async def _drain_heartbeat_children(children: tuple[asyncio.Task, ...]) -> None:
        if children:
            await asyncio.gather(*children, return_exceptions=True)

    async def _heartbeat_sender(self, websocket: WebSocketPort) -> None:
        sequence = 0
        loop = asyncio.get_running_loop()
        next_send = loop.time() + _HEARTBEAT_SECONDS
        while True:
            await asyncio.sleep(max(0.0, next_send - loop.time()))
            await self._send(
                websocket,
                _dump({"security": _SECURITY, "kind": "HEARTBEAT", "sequence": sequence}),
                timeout=_CONTROL_LOST_SECONDS,
            )
            sequence += 1
            next_send += _HEARTBEAT_SECONDS

    async def _receive(self, websocket: WebSocketPort, *, timeout: float) -> dict:
        if timeout <= 0:
            raise TimeoutError
        text = await asyncio.wait_for(websocket.receive_text(), timeout=timeout)
        return _parse(text)

    async def _send(self, websocket: WebSocketPort, data: str, *, timeout: float) -> None:
        if timeout <= 0:
            raise TimeoutError
        await asyncio.wait_for(websocket.send_text(data), timeout=timeout)

    def _hello_peer(self, hello: dict) -> AllowedPeer:
        _exact(
            hello,
            {
                "security",
                "kind",
                "peerRole",
                "peerInstallId",
                "hubInstallId",
                "keyId",
                "protocol",
                "modHash",
                "configHash",
                "bindingHash",
                "peerNonce",
            },
            "HELLO",
        )
        peer = self._peers.get((hello["peerRole"], hello["peerInstallId"]))
        if peer is None:
            raise TransportError("peer is not allowlisted")
        expected = peer.context
        checks = {
            "hubInstallId": expected.hub_install_id,
            "keyId": expected.key_id,
            "protocol": expected.protocol,
            "modHash": b32_encode(expected.mod_hash),
            "configHash": b32_encode(expected.config_hash),
            "bindingHash": b32_encode(expected.binding_hash),
        }
        if hello["hubInstallId"] != self._hub_install_id or any(hello[name] != value for name, value in checks.items()):
            raise TransportError("hello binding mismatch")
        b32_decode(hello["peerNonce"])
        return peer

    def _admit_attempt(self, install_id: str) -> None:
        now = self._monotonic()
        if not self._global_attempts.take(now) or not self._attempts[install_id].take(now):
            raise TransportError("authentication rate limit")

    def _monotonic(self) -> float:
        if self._clock_failed:
            raise TransportError("monotonic clock previously failed")
        value = self._monotonic_now()
        if type(value) not in (int, float) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
            self._clock_failed = True
            raise TransportError("invalid monotonic clock")
        value = float(value)
        if self._last_monotonic is not None and value < self._last_monotonic:
            self._clock_failed = True
            raise TransportError("monotonic clock rolled back")
        self._last_monotonic = value
        return value

    def _reserve_unauthenticated(self, connection_id: str) -> None:
        if self._clock_failed or len(self._unauthenticated) >= 4:
            raise TransportError("unauthenticated connection capacity")
        self._unauthenticated.add(connection_id)

    def _release(self, connection_id: str, role: str | None) -> None:
        self._pending.discard(connection_id)
        self._unauthenticated.discard(connection_id)
        if role is not None and self._roles.get(role, (None, None))[0] == connection_id:
            self._roles.pop(role, None)

    @staticmethod
    def _remaining(loop_deadline: float) -> float:
        return loop_deadline - asyncio.get_running_loop().time()

    def _trusted_utc(self) -> datetime:
        value = self._utc_now()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise TransportError("invalid UTC clock")
        return value.astimezone(timezone.utc)


class _Bucket:
    def __init__(self, rate: float, capacity: float) -> None:
        self.rate, self.capacity = rate, capacity
        self.tokens = capacity
        self.updated: float | None = None

    def take(self, now: float) -> bool:
        if self.updated is not None:
            if now < self.updated:
                return False
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True


def _parse(text: str) -> dict:
    if not isinstance(text, str) or len(text.encode("utf-8")) > _MAX_FRAME_BYTES:
        raise TransportError("security frame exceeds limit")
    try:
        value = json.loads(
            text, object_pairs_hook=_unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
        )
    except (json.JSONDecodeError, UnicodeError, ValueError, TypeError) as error:
        raise TransportError("invalid security JSON") from error
    _depth(value)
    if not isinstance(value, dict):
        raise TransportError("security frame must be object")
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate security key")
        result[key] = value
    return result


def _depth(value, level=0):
    if level > _MAX_DEPTH:
        raise TransportError("security JSON depth exceeds limit")
    if isinstance(value, dict):
        for item in value.values():
            _depth(item, level + 1)
    elif isinstance(value, list):
        for item in value:
            _depth(item, level + 1)


def _exact(value: dict, keys: set[str], kind: str) -> None:
    if set(value) != keys or value.get("security") != _SECURITY or value.get("kind") != kind:
        raise TransportError("security message is not allowed")


def _dump(value: dict) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True)


def _utc(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


async def _safe_close(websocket: WebSocketPort, code: int, reason: str) -> None:
    try:
        await asyncio.wait_for(websocket.close(code=code, reason=reason), timeout=_CLOSE_TIMEOUT_SECONDS)
    except Exception:
        pass
