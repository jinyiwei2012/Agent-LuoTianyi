"""Bounded, non-authoritative BODY notice routing for authenticated v2 peers."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable
from uuid import uuid4

from .current_roles import AuthenticatedControlRoleOwner, AuthenticatedRoleLease
from .postauth_frame import PostAuthFrame

_SERVER = "SERVER_AUTHORITY"
_CLIENT = "CLIENT_CONTROLLER"
_CAPACITY = 2
_TTL_MS = 5000
_I63_MAX = (1 << 63) - 1
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_B64 = re.compile(r"[A-Za-z0-9_-]{43}")
_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z")


class V2BodyRouteError(ValueError):
    """Sanitized protocol failure for one authenticated BODY route half."""


class V2BodyRouteState(str, Enum):
    ROUTED = "ROUTED"
    CONSUMED = "CONSUMED"
    INVALIDATED = "INVALIDATED"


@dataclass(frozen=True, slots=True, repr=False)
class NonAuthoritativeBodyBoundCandidate:
    route_id: str
    server_boot_id: str
    game_connection_id: str
    body_id: str
    challenge_expires_at: str


@dataclass(slots=True, repr=False)
class _Route:
    route_id: str
    server_boot_id: str
    game_connection_id: str
    body_id: str
    game_nonce: str
    payload: bytes
    controller_lease: AuthenticatedRoleLease
    server_lease: AuthenticatedRoleLease
    created_at: int
    deadline: int
    state: V2BodyRouteState = V2BodyRouteState.ROUTED
    challenge_expires_at: str = ""
    candidate: NonAuthoritativeBodyBoundCandidate | None = None


class V2BodyRouteOwner:
    """Single-owner bounded route table; candidates grant no binding authority."""

    def __init__(
        self,
        *,
        role_owner: AuthenticatedControlRoleOwner,
        monotonic_millis: Callable[[], int],
        utc_now: Callable[[], datetime],
        route_id: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(role_owner, AuthenticatedControlRoleOwner):
            raise TypeError("route owner requires authenticated roles")
        if not callable(monotonic_millis) or not callable(utc_now):
            raise TypeError("route owner requires trusted clocks")
        self._roles = role_owner
        self._clock = monotonic_millis
        self._utc_clock = utc_now
        self._route_id = route_id or (lambda: str(uuid4()))
        self._routes: dict[str, _Route] = {}
        self._generation = 0
        self._last_now: int | None = None

    def route_notice(
        self,
        controller: AuthenticatedRoleLease,
        frame: PostAuthFrame,
        send_route: Callable[[str, str, str, bytes], None],
    ) -> None:
        self._roles.assert_owner()
        _notice_frame(frame, controller)
        notice = _payload(frame.payload, notice=True)
        now = self._now()
        self._prune(now)
        if not self._roles.is_current(controller) or controller.role != _CLIENT:
            raise V2BodyRouteError("controller route origin is not current")
        server = self._roles.current(_SERVER)
        if server is None:
            raise V2BodyRouteError("server route recipient is unavailable")
        if len(self._routes) >= _CAPACITY:
            raise V2BodyRouteError("route capacity")
        deadline = _deadline(now)
        generation = self._generation
        captured_controller = controller
        captured_server = server
        route_id = self._route_id()
        if (
            self._generation != generation
            or not self._roles.is_current(captured_controller)
            or not self._roles.is_current(captured_server)
            or len(self._routes) >= _CAPACITY
            or not _uuid(route_id)
            or route_id in self._routes
        ):
            raise V2BodyRouteError("route identity unavailable")
        record = _Route(
            route_id=route_id,
            server_boot_id=notice["serverBootId"],
            game_connection_id=notice["gameConnectionId"],
            body_id=notice["bodyId"],
            game_nonce=notice["gameNonce"],
            payload=frame.payload,
            controller_lease=controller,
            server_lease=server,
            created_at=now,
            deadline=deadline,
        )
        self._routes[route_id] = record
        self._generation += 1
        published_generation = self._generation
        if (
            self._routes.get(route_id) is not record
            or record.state is not V2BodyRouteState.ROUTED
            or self._generation != published_generation
            or not self._roles.is_current(captured_controller)
            or not self._roles.is_current(captured_server)
        ):
            self._invalidate(record)
            raise V2BodyRouteError("route publication lost currentness")
        try:
            send_route(route_id, controller.handshake.context.peer_install_id, controller.connection_id, frame.payload)
        except (KeyboardInterrupt, SystemExit):
            self._invalidate(record)
            raise
        except Exception:
            self._invalidate(record)
            return
        if (
            self._routes.get(route_id) is not record
            or record.state is not V2BodyRouteState.ROUTED
            or self._generation != published_generation
            or not self._roles.is_current(controller)
            or not self._roles.is_current(server)
        ):
            self._invalidate(record)

    def accept_candidate(
        self, server: AuthenticatedRoleLease, frame: PostAuthFrame
    ) -> NonAuthoritativeBodyBoundCandidate:
        self._roles.assert_owner()
        _candidate_frame(frame, server)
        value = _payload(frame.payload, notice=False)
        expires = _utc(frame.challenge_expires_at)
        if not self._roles.is_current(server) or server.role != _SERVER:
            raise V2BodyRouteError("server candidate origin is not current")
        now = self._now()
        utc_now = self._utc()
        self._prune(now)
        if not self._roles.is_current(server) or server.role != _SERVER:
            raise V2BodyRouteError("server candidate origin is not current")
        record = self._routes.get(frame.route_id)
        if record is None:
            raise V2BodyRouteError("unknown body route")
        if record.server_lease is not server:
            raise V2BodyRouteError("body candidate does not match route")
        try:
            valid = (
                record.state is V2BodyRouteState.ROUTED
                and self._roles.is_current(record.controller_lease)
                and now < record.deadline
                and utc_now < expires
                and value["serverBootId"] == record.server_boot_id
                and value["gameConnectionId"] == record.game_connection_id
                and value["bodyId"] == record.body_id
            )
            if not valid:
                raise V2BodyRouteError("body candidate does not match route")
        except BaseException:
            self._invalidate(record)
            raise
        candidate = NonAuthoritativeBodyBoundCandidate(
            route_id=record.route_id,
            server_boot_id=record.server_boot_id,
            game_connection_id=record.game_connection_id,
            body_id=record.body_id,
            challenge_expires_at=frame.challenge_expires_at,
        )
        record.challenge_expires_at = frame.challenge_expires_at
        record.candidate = candidate
        self._transition(record, V2BodyRouteState.CONSUMED)
        return candidate

    def invalidate_lease(self, lease: AuthenticatedRoleLease) -> None:
        self._roles.assert_owner()
        for record in self._routes.values():
            if record.controller_lease is lease or record.server_lease is lease:
                self._invalidate(record)

    def prune(self) -> None:
        self._roles.assert_owner()
        self._prune(self._now())

    def candidates(self) -> tuple[NonAuthoritativeBodyBoundCandidate, ...]:
        self._roles.assert_owner()
        now = self._now()
        utc_now = self._utc()
        self._prune(now)
        result = []
        for record in self._routes.values():
            if (
                record.state is V2BodyRouteState.CONSUMED
                and record.candidate is not None
                and self._roles.is_current(record.server_lease)
                and self._roles.is_current(record.controller_lease)
                and now < record.deadline
                and utc_now < _utc(record.challenge_expires_at)
            ):
                result.append(record.candidate)
        return tuple(result)

    def _prune(self, now: int) -> None:
        for route_id, record in tuple(self._routes.items()):
            if now >= record.deadline:
                self._invalidate(record)
                self._routes.pop(route_id, None)
                self._generation += 1

    def _invalidate(self, record: _Route) -> None:
        self._transition(record, V2BodyRouteState.INVALIDATED)

    def _transition(self, record: _Route, state: V2BodyRouteState) -> None:
        if record.state is not state:
            record.state = state
            self._generation += 1

    def _now(self) -> int:
        value = self._clock()
        if (
            type(value) is not int
            or not 0 <= value <= _I63_MAX
            or (self._last_now is not None and value < self._last_now)
        ):
            raise V2BodyRouteError("invalid route clock")
        self._last_now = value
        return value

    def _utc(self) -> datetime:
        value = self._utc_clock()
        if (
            type(value) is not datetime
            or value.tzinfo is None
            or value.utcoffset() != timedelta(0)
            or value.microsecond % 1000 != 0
        ):
            raise V2BodyRouteError("invalid route UTC")
        return value.astimezone(timezone.utc)


def _deadline(now: int) -> int:
    if now > _I63_MAX - _TTL_MS:
        raise V2BodyRouteError("route deadline overflow")
    return now + _TTL_MS


def _uuid(value: object) -> bool:
    return type(value) is str and _UUID.fullmatch(value) is not None and value != "00000000-0000-0000-0000-000000000000"


def _utc(value: object) -> datetime:
    if type(value) is not str or _UTC.fullmatch(value) is None or not value.isascii():
        raise V2BodyRouteError("invalid candidate expiry")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise V2BodyRouteError("invalid candidate expiry") from error
    if parsed.microsecond % 1000:
        raise V2BodyRouteError("invalid candidate expiry")
    return parsed


def _notice_frame(frame: object, controller: AuthenticatedRoleLease) -> None:
    if (
        not isinstance(frame, PostAuthFrame)
        or controller.role != _CLIENT
        or frame.message_kind != "BODY_BIND_NOTICE"
        or frame.connection_id != controller.connection_id
        or any(
            (
                frame.route_id,
                frame.source_install_id,
                frame.source_connection_id,
                frame.recipient_connection_id,
                frame.challenge_expires_at,
            )
        )
    ):
        raise V2BodyRouteError("invalid body notice metadata")


def _candidate_frame(frame: object, server: AuthenticatedRoleLease) -> None:
    if (
        not isinstance(frame, PostAuthFrame)
        or server.role != _SERVER
        or frame.message_kind != "BODY_BOUND_CANDIDATE"
        or frame.connection_id != server.connection_id
        or not _uuid(frame.route_id)
        or frame.source_install_id
        or frame.source_connection_id
        or frame.recipient_connection_id != server.connection_id
    ):
        raise V2BodyRouteError("invalid body candidate metadata")
    _utc(frame.challenge_expires_at)


def _payload(raw: bytes, *, notice: bool) -> dict[str, str]:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise V2BodyRouteError("invalid body payload") from error
    keys = {"security", "kind", "serverBootId", "gameConnectionId", "bodyId"}
    if notice:
        keys.add("gameNonce")
    if (
        type(value) is not dict
        or set(value) != keys
        or any(type(item) is not str for item in value.values())
        or value["security"] != "luosandbox.control-auth.v1"
        or value["kind"] != ("BODY_BIND_NOTICE" if notice else "BODY_BOUND")
        or not all(_uuid(value[key]) for key in ("serverBootId", "gameConnectionId", "bodyId"))
    ):
        raise V2BodyRouteError("invalid body payload")
    if notice:
        nonce = value["gameNonce"]
        try:
            decoded = base64.urlsafe_b64decode(nonce + "=" * (-len(nonce) % 4))
        except (ValueError, UnicodeError) as error:
            raise V2BodyRouteError("invalid body nonce") from error
        if (
            not _B64.fullmatch(nonce)
            or len(decoded) != 32
            or base64.urlsafe_b64encode(decoded).decode().rstrip("=") != nonce
        ):
            raise V2BodyRouteError("invalid body nonce")
    return value
