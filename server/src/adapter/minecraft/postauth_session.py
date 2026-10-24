"""Unassembled, owner-thread v2 Hub post-auth session state machine.

This module owns only one already-authenticated connection's codec sequencing.  It
does not own sockets, role maps, routes, bindings, or application authority.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol, runtime_checkable

from .postauth_frame import PostAuthFrame, TrustedFrameContext, encode_frame, parse_frame
from .postauth_keys import AuthenticatedTranscript, PostAuthSessionKeys

_OPEN_TIMEOUT_MS = 5000
_HEARTBEAT_INTERVAL_MS = 500
_HEARTBEAT_TIMEOUT_MS = 2000
_QUEUE_CAPACITY = 8
_I63_MAX = (1 << 63) - 1


class PostAuthSessionError(ValueError):
    """Raised when a caller violates the unassembled Hub session contract."""


@runtime_checkable
class _Completion(Protocol):
    def add_done_callback(self, callback: Callable[[object], None]) -> object: ...

    def cancelled(self) -> bool: ...

    def exception(self) -> BaseException | None: ...


class PostAuthSessionState(str, Enum):
    NEW = "NEW"
    WAIT_OPEN_ACK = "WAIT_OPEN_ACK"
    APPLICATION_READY = "APPLICATION_READY"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class PostAuthSessionEvent:
    """Decoded, non-authoritative application input after v2 codec verification."""

    frame: PostAuthFrame


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class _Entry:
    generation: int
    sequence: int
    raw: bytes


class PostAuthHubSession:
    """One Hub-side session driven explicitly on its creating thread.

    ``fence`` is invoked synchronously before ``close``.  Assemblers must use it
    in their same owner turn to remove the exact current role and local authority.
    """

    def __init__(
        self,
        *,
        authenticated: AuthenticatedTranscript,
        keys: PostAuthSessionKeys,
        connection_id: str,
        monotonic_millis: Callable[[], int],
        send: Callable[[bytes], _Completion],
        post: Callable[[Callable[[], None]], None],
        owner_abort: Callable[[Callable[[], None]], None],
        report_cleanup_unknown: Callable[[], None],
        fence: Callable[[], None],
        close: Callable[[], None],
        observe: Callable[[PostAuthSessionEvent], None],
    ) -> None:
        self._owner = threading.get_ident()
        self._authenticated = authenticated
        self._keys = keys
        self._context = TrustedFrameContext(authenticated=authenticated, connection_id=connection_id)
        self._clock = monotonic_millis
        self._send = send
        self._post = post
        self._owner_abort = owner_abort
        self._report_cleanup_unknown = report_cleanup_unknown
        self._fence = fence
        self._close = close
        self._observe = observe
        self._state = PostAuthSessionState.NEW
        self._generation = 0
        self._outbound: list[_Entry] = []
        self._inflight: _Entry | None = None
        self._next_outbound = 0
        self._next_inbound = 0
        self._open_deadline: int | None = None
        self._next_heartbeat: int | None = None
        self._watchdog_deadline: int | None = None
        self._last_now: int | None = None
        self._keys.require_context(authenticated)

    @property
    def state(self) -> PostAuthSessionState:
        self._owner_call()
        return self._state

    def start(self) -> None:
        self._owner_call()
        if self._state is not PostAuthSessionState.NEW:
            raise PostAuthSessionError("session already started")
        try:
            now = self._now()
            deadline = _deadline(now, _OPEN_TIMEOUT_MS)
            payload = (
                b'{"security":"luosandbox.control-auth.v1","kind":"SESSION_OPEN","connectionId":"'
                + self._context.connection_id.encode("ascii")
                + b'"}'
            )
            entry = self._encode_entry("SESSION_OPEN", payload, "HUB_TO_PEER")
            self._outbound.append(entry)
            self._next_outbound = _increment(self._next_outbound)
            self._state = PostAuthSessionState.WAIT_OPEN_ACK
            self._open_deadline = deadline
            self._drain()
        except BaseException:
            self._fail_closed()
            raise

    def receive(self, raw: bytes) -> None:
        self._owner_call()
        if self._state is PostAuthSessionState.CLOSED:
            raise PostAuthSessionError("session closed")
        try:
            now = self._now()
            self._check_liveness(now)
            frame = parse_frame(raw, self._context, self._keys, "PEER_TO_HUB")
            if frame.sequence != self._next_inbound:
                raise PostAuthSessionError("inbound sequence mismatch")
            next_inbound = _increment(frame.sequence)
            if self._state is PostAuthSessionState.WAIT_OPEN_ACK:
                if frame.sequence != 0 or frame.message_kind != "HEARTBEAT" or frame.payload != b"{}":
                    raise PostAuthSessionError("open acknowledgement required")
                self._state = PostAuthSessionState.APPLICATION_READY
                self._next_heartbeat = _deadline(now, _HEARTBEAT_INTERVAL_MS)
                self._watchdog_deadline = _deadline(now, _HEARTBEAT_TIMEOUT_MS)
                self._next_inbound = next_inbound
            elif self._state is not PostAuthSessionState.APPLICATION_READY:
                raise PostAuthSessionError("session not ready")
            elif frame.message_kind == "HEARTBEAT":
                self._watchdog_deadline = _deadline(now, _HEARTBEAT_TIMEOUT_MS)
                self._next_inbound = next_inbound
            else:
                generation = self._generation
                self._next_inbound = next_inbound
                self._observe(PostAuthSessionEvent(frame=frame))
                if self._state is PostAuthSessionState.CLOSED or generation != self._generation:
                    return
        except BaseException:
            self._fail_closed()
            raise

    def tick(self) -> None:
        self._owner_call()
        if self._state is PostAuthSessionState.CLOSED:
            return
        try:
            now = self._now()
            self._check_liveness(now)
            if self._state is PostAuthSessionState.APPLICATION_READY:
                if now >= self._required(self._watchdog_deadline):
                    raise PostAuthSessionError("heartbeat timed out")
                if now >= self._required(self._next_heartbeat):
                    self._admit("HEARTBEAT", b"{}", "HUB_TO_PEER", now)
                    self._next_heartbeat = _deadline(now, _HEARTBEAT_INTERVAL_MS)
        except BaseException:
            self._fail_closed()
            raise

    def close(self) -> None:
        self._owner_call()
        self._fail_closed()

    def send_body_bind_route(
        self,
        route_id: str,
        source_install_id: str,
        source_connection_id: str,
        payload: bytes,
    ) -> None:
        self._owner_call()
        try:
            now = self._now()
            if (
                self._state is not PostAuthSessionState.APPLICATION_READY
                or self._authenticated.context.peer_role != "SERVER_AUTHORITY"
            ):
                raise PostAuthSessionError("server route session is not ready")
            self._check_liveness(now)
            self._admit(
                "BODY_BIND_ROUTE",
                payload,
                "HUB_TO_PEER",
                now,
                route_id=route_id,
                source_install_id=source_install_id,
                source_connection_id=source_connection_id,
                recipient_connection_id=self._context.connection_id or "",
            )
        except BaseException:
            self._fail_closed()
            raise

    def _admit(self, kind: str, payload: bytes, direction: str, now: int, **metadata: str) -> None:
        self._check_liveness(now)
        entry = self._encode_entry(kind, payload, direction, **metadata)
        self._outbound.append(entry)
        self._next_outbound = _increment(self._next_outbound)
        self._drain()

    def _encode_entry(self, kind: str, payload: bytes, direction: str, **metadata: str) -> _Entry:
        if len(self._outbound) >= _QUEUE_CAPACITY or self._next_outbound > _I63_MAX:
            raise PostAuthSessionError("outbound queue unavailable")
        frame = PostAuthFrame(
            connection_id=self._context.connection_id or "",
            challenge_id=self._authenticated.challenge_id,
            sequence=self._next_outbound,
            message_kind=kind,
            route_id=metadata.get("route_id", ""),
            source_install_id=metadata.get("source_install_id", ""),
            source_connection_id=metadata.get("source_connection_id", ""),
            recipient_connection_id=metadata.get("recipient_connection_id", ""),
            challenge_expires_at=metadata.get("challenge_expires_at", ""),
            payload=payload,
        )
        raw = encode_frame(frame, self._context, self._keys, direction)
        return _Entry(generation=self._generation, sequence=self._next_outbound, raw=raw)

    def _drain(self) -> None:
        if self._inflight is not None or not self._outbound or self._state is PostAuthSessionState.CLOSED:
            return
        entry = self._outbound[0]
        self._inflight = entry
        try:
            completion = self._send(entry.raw)
            if not hasattr(completion, "add_done_callback"):
                raise PostAuthSessionError("send must return completion")
            completion.add_done_callback(lambda done: self._post_completion(entry, done))
        except BaseException:
            self._fail_closed()
            raise

    def _post_completion(self, entry: _Entry, done: object) -> None:
        try:
            self._post(lambda: self._complete(entry, done))
        except BaseException:
            self._request_owner_abort(entry.generation)

    def _complete(self, entry: _Entry, done: object) -> None:
        self._owner_call()
        if (
            self._state is PostAuthSessionState.CLOSED
            or entry.generation != self._generation
            or self._inflight != entry
        ):
            return
        try:
            if not isinstance(done, _Completion) or done.cancelled() or done.exception() is not None:
                raise PostAuthSessionError("send completion failed")
            if not self._outbound or self._outbound[0] != entry:
                raise PostAuthSessionError("send completion ordering failure")
            self._outbound.pop(0)
            self._inflight = None
            self._drain()
        except BaseException:
            self._fail_closed()
            raise

    def _request_owner_abort(self, generation: int) -> None:
        """Ask the reliable owner abort port to fence this exact generation."""
        try:
            self._owner_abort(lambda: self._abort_generation(generation))
        except BaseException:
            self._report_cleanup_unknown()

    def _abort_generation(self, generation: int) -> None:
        self._owner_call()
        if generation == self._generation and self._state is not PostAuthSessionState.CLOSED:
            self._fail_closed()

    def _fail_closed(self) -> None:
        if self._state is PostAuthSessionState.CLOSED:
            return
        self._state = PostAuthSessionState.CLOSED
        self._generation += 1
        self._outbound.clear()
        self._inflight = None
        self._open_deadline = self._next_heartbeat = self._watchdog_deadline = None
        self._keys.close()
        try:
            self._fence()
        finally:
            self._close()

    def _owner_call(self) -> None:
        if threading.get_ident() != self._owner:
            raise PostAuthSessionError("wrong owner thread")

    def _now(self) -> int:
        value = self._clock()
        if (
            type(value) is not int
            or not 0 <= value <= _I63_MAX
            or (self._last_now is not None and value < self._last_now)
        ):
            raise PostAuthSessionError("invalid monotonic clock")
        self._last_now = value
        return value

    def _check_liveness(self, now: int) -> None:
        if self._state is PostAuthSessionState.WAIT_OPEN_ACK:
            if now >= self._required(self._open_deadline):
                raise PostAuthSessionError("open acknowledgement timed out")
        elif self._state is PostAuthSessionState.APPLICATION_READY and now >= self._required(self._watchdog_deadline):
            raise PostAuthSessionError("heartbeat timed out")

    @staticmethod
    def _required(value: int | None) -> int:
        if value is None:
            raise PostAuthSessionError("missing deadline")
        return value


def _deadline(now: int, interval: int) -> int:
    if now > _I63_MAX - interval:
        raise PostAuthSessionError("monotonic deadline overflow")
    return now + interval


def _increment(value: int) -> int:
    if value > _I63_MAX:
        raise PostAuthSessionError("sequence overflow")
    return value + 1
