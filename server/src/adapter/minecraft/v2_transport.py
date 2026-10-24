"""Unregistered asyncio/WebSocket assembly for authenticated v2 peer sessions.

This module owns no listener or route.  It authenticates only privately
provisioned peers, installs one exact current-role lease, and drives the frozen
post-auth session.  Application frames are denied until a later authority
assembler supplies an explicit observer.
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import uuid4

from .control_auth import ControlAuthError, PendingHandshakes, b32_decode, b32_encode
from .control_binding import MinecraftControlBindingCoordinator
from .current_roles import AuthenticatedControlRoleOwner, CurrentRoleError
from .postauth_keys import AuthenticatedTranscript, PostAuthKeyError
from .postauth_session import PostAuthHubSession, PostAuthSessionError, PostAuthSessionState
from .transport import WebSocketPort
from .v2_body_routes import V2BodyRouteError, V2BodyRouteOwner
from .v2_startup import ProvisionedV2Peer, V2StartupError

_SECURITY = "luosandbox.control-auth.v1"
_SUBPROTOCOL = "LUO_CONTROL_POSTAUTH_V2"
_MAX_FRAME_BYTES = 4096
_MAX_DEPTH = 4
_BOOTSTRAP_SECONDS = 5.0
_TICK_SECONDS = 0.05
_CLOSE_TIMEOUT_SECONDS = 0.1
_I63_MAX = (1 << 63) - 1
# Keep adjacent millisecond samples distinguishable after conversion to the
# shared auth API's float seconds, with headroom for its fixed five-second TTL.
_MAX_SAFE_AUTH_MILLIS = ((1 << 42) - 5) * 1000 - 1


class V2TransportError(RuntimeError):
    """Sanitized v2 transport assembly failure."""


class MinecraftV2AuthenticatedTransport:
    """Owner-loop v2 authentication and post-auth session assembly."""

    def __init__(
        self,
        *,
        peers: tuple[ProvisionedV2Peer, ...],
        hub_install_id: str,
        monotonic_millis: Callable[[], int],
        utc_now: Callable[[], datetime],
        nonce: Callable[[], bytes] | None = None,
        challenge_id: Callable[[], str] | None = None,
        role_owner: AuthenticatedControlRoleOwner | None = None,
        binding_coordinator: MinecraftControlBindingCoordinator | None = None,
    ) -> None:
        if type(peers) is not tuple or not peers or type(hub_install_id) is not str or not hub_install_id:
            raise ValueError("invalid v2 transport provisioning")
        if not callable(monotonic_millis) or not callable(utc_now):
            raise ValueError("v2 transport requires trusted clocks")
        selected: dict[tuple[str, str], ProvisionedV2Peer] = {}
        roles: set[str] = set()
        installs: set[str] = set()
        for peer in peers:
            if (
                type(peer) is not ProvisionedV2Peer
                or not peer.is_provisioned
                or peer.context.protocol != _SUBPROTOCOL
                or peer.context.hub_install_id != hub_install_id
                or peer.context.peer_role in roles
                or peer.context.peer_install_id in installs
            ):
                raise ValueError("invalid v2 transport peer")
            key = (peer.context.peer_role, peer.context.peer_install_id)
            selected[key] = peer
            roles.add(peer.context.peer_role)
            installs.add(peer.context.peer_install_id)
        self._peers = selected
        self._hub_install_id = hub_install_id
        self._clock = monotonic_millis
        self._utc_clock = utc_now
        self._nonce = nonce
        self._challenge_id = challenge_id
        contexts = tuple(peer.context for peer in peers)
        if binding_coordinator is not None and (role_owner is None or binding_coordinator.role_owner is not role_owner):
            raise ValueError("binding coordinator role owner mismatch")
        if role_owner is not None and not isinstance(role_owner, AuthenticatedControlRoleOwner):
            raise ValueError("invalid v2 role owner")
        selected_owner = role_owner or AuthenticatedControlRoleOwner(contexts, hub_install_id)
        if not selected_owner.matches_contexts(contexts, hub_install_id):
            raise ValueError("v2 role owner provisioning mismatch")
        self._pending = PendingHandshakes(maximum=4)
        self._roles = selected_owner
        self._binding = binding_coordinator
        self._body_routes = V2BodyRouteOwner(
            role_owner=selected_owner,
            monotonic_millis=self._monotonic_millis,
            utc_now=self._trusted_utc,
        )
        self._sessions: dict[str, tuple[object, PostAuthHubSession]] = {}
        self._unauthenticated: set[str] = set()
        self._attempts = {install: _Bucket(1.0, 2.0) for install in installs}
        self._global_attempts = _Bucket(2.0, 4.0)
        self._last_millis: int | None = None
        self._clock_failed = False

    @property
    def active_roles(self) -> tuple[str, ...]:
        return self._roles.active_roles()

    @property
    def pending_count(self) -> int:
        return len(self._unauthenticated)

    async def handle(self, websocket: WebSocketPort) -> None:  # noqa: C901
        connection_id = str(uuid4())
        reservation = lease = None
        session: PostAuthHubSession | None = None
        keys = None
        send_tasks: set[asyncio.Task] = set()
        cleanup_tasks: set[asyncio.Task] = set()
        close_requested = asyncio.Event()
        callback_error: list[BaseException] = []
        loop = asyncio.get_running_loop()
        owner_thread = threading.get_ident()
        cancellation: asyncio.CancelledError | None = None
        fatal: BaseException | None = None
        cleanup_failed = False
        close_code, close_reason = 1008, "FEATURE_DISABLED"
        try:
            self._reserve_unauthenticated(connection_id)
            bootstrap_deadline = loop.time() + _BOOTSTRAP_SECONDS
            await websocket.accept(subprotocol=_SUBPROTOCOL)
            hello = await self._receive_plain(websocket, self._remaining(bootstrap_deadline))
            peer = self._hello_peer(hello)
            self._admit_attempt(peer.context.peer_install_id)
            reservation = self._roles.reserve(peer.context, connection_id)
            issued = self._trusted_utc()
            now_millis = self._monotonic_millis()
            pending, hub_proof = self._pending.begin_v2(
                connection_id,
                peer,
                b32_decode(hello["peerNonce"]),
                issued,
                hub_nonce=None if self._nonce is None else self._nonce(),
                challenge_id=None if self._challenge_id is None else self._challenge_id(),
                now=now_millis / 1000.0,
                utc_now=issued,
            )
            self._roles.bind_challenge(reservation, pending.challenge_id)
            await self._send_plain(
                websocket,
                _dump(
                    {
                        "security": _SECURITY,
                        "kind": "CHALLENGE",
                        "challengeId": pending.challenge_id,
                        "hubNonce": b32_encode(pending.hub_nonce),
                        "issuedAt": _utc(pending.issued_at),
                        "expiresAt": _utc(pending.expires_at),
                        "hubProof": hub_proof,
                    }
                ),
                self._remaining(bootstrap_deadline),
                send_tasks,
            )
            proof = await self._receive_plain(
                websocket,
                min(self._remaining(bootstrap_deadline), max(0.0, pending.deadline - self._auth_seconds())),
            )
            _exact(proof, {"security", "kind", "challengeId", "peerProof"}, "PROOF")
            handshake = self._pending.consume_v2(
                connection_id,
                proof["challengeId"],
                proof["peerProof"],
                peer,
                expected_context=peer.context,
                now=self._auth_seconds(),
                utc_now=self._trusted_utc(),
            )
            await self._send_plain(
                websocket,
                _dump(
                    {
                        "security": _SECURITY,
                        "kind": "READY",
                        "challengeId": handshake.challenge_id,
                        "readyProof": handshake.ready_proof,
                    }
                ),
                self._remaining(bootstrap_deadline),
                send_tasks,
            )
            lease = self._roles.promote(reservation, handshake)
            reservation = None
            self._unauthenticated.discard(connection_id)
            authenticated = AuthenticatedTranscript(
                context=handshake.context,
                peer_nonce=handshake.peer_nonce,
                hub_nonce=handshake.hub_nonce,
                challenge_id=handshake.challenge_id,
                issued_at=handshake.issued_at,
                expires_at=handshake.expires_at,
            )
            keys = peer.derive_session_keys(authenticated)

            def fence() -> None:
                nonlocal cleanup_failed, fatal, lease
                if lease is not None:
                    try:
                        self._withdraw_role(lease, cleanup_tasks)
                    except BaseException as error:
                        if isinstance(error, (KeyboardInterrupt, SystemExit)):
                            fatal = fatal or error
                        else:
                            cleanup_failed = True
                        raise
                    finally:
                        lease = None

            def request_close() -> None:
                close_requested.set()

            def post(callback) -> None:
                if loop.is_closed():
                    raise V2TransportError("session owner loop unavailable")

                def guarded() -> None:
                    try:
                        callback()
                    except BaseException as error:
                        callback_error.append(error)
                        close_requested.set()

                loop.call_soon_threadsafe(guarded)

            def owner_abort(callback) -> None:
                post(callback)

            def report_cleanup_unknown() -> None:
                callback_error.append(V2TransportError("session cleanup outcome is unknown"))
                loop.call_soon_threadsafe(close_requested.set)

            def send(raw: bytes):
                if threading.get_ident() != owner_thread or loop.is_closed():
                    raise V2TransportError("wrong session owner")
                task = loop.create_task(websocket.send_text(raw.decode("ascii")), name="minecraft-v2-session-send")
                send_tasks.add(task)
                task.add_done_callback(send_tasks.discard)
                return task

            def observe_application(event) -> None:
                if lease is None or session is None or not self._roles.is_current(lease):
                    raise V2TransportError("application session is not current")
                association = self._sessions.get(lease.role)
                if association != (lease, session) or session.state is not PostAuthSessionState.APPLICATION_READY:
                    raise V2TransportError("application session is not ready")
                frame = event.frame
                if frame.message_kind == "BODY_BIND_NOTICE" and lease.role == "CLIENT_CONTROLLER":
                    server_association = self._sessions.get("SERVER_AUTHORITY")
                    if server_association is None:
                        raise V2BodyRouteError("server route recipient is unavailable")
                    server_lease, server_session = server_association
                    if (
                        not self._roles.is_current(server_lease)
                        or server_session.state is not PostAuthSessionState.APPLICATION_READY
                    ):
                        raise V2BodyRouteError("server route recipient is unavailable")
                    self._body_routes.route_notice(
                        lease,
                        frame,
                        server_session.send_body_bind_route,
                    )
                    return
                if frame.message_kind == "BODY_BOUND_CANDIDATE" and lease.role == "SERVER_AUTHORITY":
                    self._body_routes.accept_candidate(lease, frame)
                    return
                raise V2TransportError("v2 application authority is disabled")

            session = PostAuthHubSession(
                authenticated=authenticated,
                keys=keys,
                connection_id=connection_id,
                monotonic_millis=self._monotonic_millis,
                send=send,
                post=post,
                owner_abort=owner_abort,
                report_cleanup_unknown=report_cleanup_unknown,
                fence=fence,
                close=request_close,
                observe=observe_application,
            )
            session.start()
            self._sessions[lease.role] = (lease, session)
            await self._drive_session(websocket, session, close_requested, callback_error)
        except asyncio.CancelledError as error:
            cancellation = error
            close_code, close_reason = 1001, "FEATURE_DISABLED"
        except (
            V2TransportError,
            V2StartupError,
            ControlAuthError,
            CurrentRoleError,
            PostAuthKeyError,
            PostAuthSessionError,
            TimeoutError,
            asyncio.TimeoutError,
            UnicodeError,
            ValueError,
            TypeError,
            RuntimeError,
        ):
            pass
        except BaseException as error:
            fatal = error
        finally:
            if session is not None:
                try:
                    session.close()
                except BaseException as error:
                    if isinstance(error, (KeyboardInterrupt, SystemExit)):
                        fatal = fatal or error
                    else:
                        cleanup_failed = True
            elif keys is not None:
                try:
                    keys.close()
                except BaseException as error:
                    if isinstance(error, (KeyboardInterrupt, SystemExit)):
                        fatal = fatal or error
                    else:
                        cleanup_failed = True
            if lease is not None:
                try:
                    self._withdraw_role(lease, cleanup_tasks)
                except BaseException as error:
                    if isinstance(error, (KeyboardInterrupt, SystemExit)):
                        fatal = fatal or error
                    else:
                        cleanup_failed = True
            if reservation is not None:
                try:
                    self._roles.withdraw(reservation)
                except BaseException as error:
                    if isinstance(error, (KeyboardInterrupt, SystemExit)):
                        fatal = fatal or error
                    else:
                        cleanup_failed = True
            self._pending.discard(connection_id)
            self._unauthenticated.discard(connection_id)
            cleanup_cancel = await _cancel_and_drain(tuple(send_tasks))
            cleanup_cancel = cleanup_cancel or await _drain_tasks(tuple(cleanup_tasks))
            await _safe_close(websocket, close_code, close_reason)
            cancellation = cancellation or cleanup_cancel
        if fatal is not None:
            raise fatal
        if cancellation is not None:
            raise cancellation
        if cleanup_failed:
            raise V2TransportError("v2 transport cleanup failed") from None

    async def _drive_session(
        self,
        websocket: WebSocketPort,
        session: PostAuthHubSession,
        close_requested: asyncio.Event,
        callback_error: list[BaseException],
    ) -> None:
        receive: asyncio.Task | None = None
        try:
            while session.state is not PostAuthSessionState.CLOSED and not close_requested.is_set():
                if callback_error:
                    raise V2TransportError("session callback failed")
                if receive is None:
                    receive = asyncio.create_task(websocket.receive_text(), name="minecraft-v2-session-receive")
                done, _ = await asyncio.wait((receive,), timeout=_TICK_SECONDS)
                if receive in done:
                    text = receive.result()
                    receive = None
                    raw = text.encode("utf-8")
                    if not raw or len(raw) > _MAX_FRAME_BYTES:
                        raise V2TransportError("authenticated frame exceeds limit")
                    session.receive(raw)
                session.tick()
                self._body_routes.prune()
        finally:
            if receive is not None:
                cancellation = await _cancel_and_drain((receive,))
                if cancellation is not None:
                    raise cancellation

    def _withdraw_role(self, lease, cleanup_tasks: set[asyncio.Task]) -> None:  # noqa: C901
        def after_remove(withdrawal) -> None:  # noqa: C901
            first_error = None
            fatal = None
            try:
                association = self._sessions.get(lease.role)
                if association is not None and association[0] is lease:
                    self._sessions.pop(lease.role, None)
                self._body_routes.invalidate_lease(lease)
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    fatal = error
                else:
                    first_error = error
            try:
                if self._binding is not None:
                    command = self._binding.fence_role_withdrawal(withdrawal)
                    if command is not None:
                        task = asyncio.create_task(
                            self._binding.notify_revoke(command),
                            name="minecraft-v2-binding-revoke",
                        )
                        cleanup_tasks.add(task)
                        task.add_done_callback(cleanup_tasks.discard)
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    fatal = fatal or error
                elif first_error is None:
                    first_error = error
            if fatal is not None:
                raise fatal
            if first_error is not None:
                raise first_error

        self._roles.withdraw(lease, after_remove=after_remove)

    async def _receive_plain(self, websocket: WebSocketPort, timeout: float) -> dict:
        if timeout <= 0:
            raise TimeoutError
        return _parse(await asyncio.wait_for(websocket.receive_text(), timeout=timeout))

    @staticmethod
    async def _send_plain(websocket: WebSocketPort, data: str, timeout: float, tasks: set[asyncio.Task]) -> None:
        if timeout <= 0 or tasks:
            raise V2TransportError("plain send unavailable")
        task = asyncio.create_task(websocket.send_text(data), name="minecraft-v2-bootstrap-send")
        tasks.add(task)
        try:
            await asyncio.wait_for(task, timeout=timeout)
        finally:
            tasks.discard(task)

    def _hello_peer(self, hello: dict) -> ProvisionedV2Peer:
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
        if peer is None or not peer.is_provisioned:
            raise V2TransportError("peer is not provisioned")
        context = peer.context
        expected = {
            "hubInstallId": self._hub_install_id,
            "keyId": context.key_id,
            "protocol": _SUBPROTOCOL,
            "modHash": b32_encode(context.mod_hash),
            "configHash": b32_encode(context.config_hash),
            "bindingHash": b32_encode(context.binding_hash),
        }
        if any(hello[name] != value for name, value in expected.items()):
            raise V2TransportError("hello binding mismatch")
        nonce = b32_decode(hello["peerNonce"])
        if not any(nonce):
            raise V2TransportError("invalid peer nonce")
        return peer

    def _admit_attempt(self, install_id: str) -> None:
        now = self._auth_seconds()
        if not self._global_attempts.take(now) or not self._attempts[install_id].take(now):
            raise V2TransportError("authentication rate limit")

    def _reserve_unauthenticated(self, connection_id: str) -> None:
        if self._clock_failed or len(self._unauthenticated) >= 4:
            raise V2TransportError("unauthenticated connection capacity")
        self._unauthenticated.add(connection_id)

    def _monotonic_millis(self) -> int:
        if self._clock_failed:
            raise V2TransportError("monotonic clock previously failed")
        value = self._clock()
        if type(value) is not int or not 0 <= value <= min(_I63_MAX, _MAX_SAFE_AUTH_MILLIS):
            self._clock_failed = True
            raise V2TransportError("invalid monotonic clock")
        if self._last_millis is not None and value < self._last_millis:
            self._clock_failed = True
            raise V2TransportError("monotonic clock rolled back")
        self._last_millis = value
        return value

    def _auth_seconds(self) -> float:
        return self._monotonic_millis() / 1000.0

    def _trusted_utc(self) -> datetime:
        value = self._utc_clock()
        if (
            type(value) is not datetime
            or value.tzinfo is None
            or value.utcoffset() != timedelta(0)
            or value.microsecond % 1000 != 0
        ):
            raise V2TransportError("invalid trusted UTC clock")
        return value.replace(tzinfo=timezone.utc)

    @staticmethod
    def _remaining(deadline: float) -> float:
        return deadline - asyncio.get_running_loop().time()


class _Bucket:
    def __init__(self, rate: float, capacity: float) -> None:
        self.rate = rate
        self.capacity = capacity
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
    if type(text) is not str:
        raise V2TransportError("security frame must be text")
    try:
        raw = text.encode("utf-8")
    except UnicodeError as error:
        raise V2TransportError("invalid security text") from error
    if not raw or len(raw) > _MAX_FRAME_BYTES:
        raise V2TransportError("security frame exceeds limit")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (json.JSONDecodeError, UnicodeError, ValueError, TypeError) as error:
        raise V2TransportError("invalid security JSON") from error
    _depth(value)
    if type(value) is not dict:
        raise V2TransportError("security frame must be object")
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
        raise V2TransportError("security JSON depth exceeds limit")
    if isinstance(value, dict):
        for item in value.values():
            _depth(item, level + 1)
    elif isinstance(value, list):
        for item in value:
            _depth(item, level + 1)


def _exact(value: dict, keys: set[str], kind: str) -> None:
    if set(value) != keys or value.get("security") != _SECURITY or value.get("kind") != kind:
        raise V2TransportError("security message is not allowed")


def _dump(value: dict) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True)


def _utc(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


async def _cancel_and_drain(tasks: tuple[asyncio.Task, ...]) -> asyncio.CancelledError | None:
    tasks = tuple(dict.fromkeys(task for task in tasks if not task.done()))
    for task in tasks:
        task.cancel()
    if not tasks:
        return None

    async def drain() -> None:
        await asyncio.gather(*tasks, return_exceptions=True)

    cleanup = asyncio.create_task(drain(), name="minecraft-v2-task-cleanup")
    cancellation = None
    while not cleanup.done():
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
    cleanup.result()
    return cancellation


async def _drain_tasks(tasks: tuple[asyncio.Task, ...]) -> asyncio.CancelledError | None:
    tasks = tuple(dict.fromkeys(tasks))
    if not tasks:
        return None

    async def drain() -> None:
        await asyncio.gather(*tasks, return_exceptions=True)

    cleanup = asyncio.create_task(drain(), name="minecraft-v2-notification-cleanup")
    cancellation = None
    while not cleanup.done():
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
    cleanup.result()
    return cancellation


async def _safe_close(websocket: WebSocketPort, code: int, reason: str) -> None:
    try:
        await asyncio.wait_for(websocket.close(code=code, reason=reason), timeout=_CLOSE_TIMEOUT_SECONDS)
    except Exception:
        pass
