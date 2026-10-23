"""Single-owner current authenticated role reservations for Minecraft control."""

from __future__ import annotations

import asyncio
import re
import threading
from dataclasses import dataclass

from .control_auth import AuthenticatedHandshake, ControlAuthContext

_ROLES = frozenset(("SERVER_AUTHORITY", "CLIENT_CONTROLLER"))
_I63_MAX = (1 << 63) - 1
_UUID = re.compile(
    r"(?!00000000-0000-0000-0000-000000000000$)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


class CurrentRoleError(RuntimeError):
    """Fail-closed role ownership error without authentication material."""


@dataclass(frozen=True, slots=True, repr=False)
class RoleWithdrawal:
    role: str
    connection_id: str
    generation: int


@dataclass(frozen=True, slots=True, repr=False)
class _RoleReservation:
    role: str
    connection_id: str
    generation: int
    context: ControlAuthContext
    identity: object


@dataclass(frozen=True, slots=True, repr=False)
class _AuthenticatedRoleLease:
    role: str
    connection_id: str
    generation: int
    handshake: AuthenticatedHandshake
    identity: object


class AuthenticatedControlRoleOwner:
    """Exact two-role owner, bound to the first calling asyncio loop and thread."""

    def __init__(self, contexts: tuple[ControlAuthContext, ...], hub_install_id: str):
        if not contexts or type(hub_install_id) is not str or not hub_install_id:
            raise ValueError("invalid provisioned role owner")
        expected: dict[str, ControlAuthContext] = {}
        for context in contexts:
            if (
                not isinstance(context, ControlAuthContext)
                or context.peer_role not in _ROLES
                or context.hub_install_id != hub_install_id
                or context.peer_role in expected
            ):
                raise ValueError("invalid provisioned role owner")
            expected[context.peer_role] = context
        self._expected = expected
        self._slots: dict[str, _RoleReservation | _AuthenticatedRoleLease] = {}
        self._challenges: dict[object, str] = {}
        self._generation = 0
        self._closed = False
        self._thread: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def reserve(self, context: ControlAuthContext, connection_id: str) -> _RoleReservation:
        self._owner()
        if self._closed or self._expected.get(context.peer_role) is not context or not _uuid(connection_id):
            raise CurrentRoleError("role context is not provisioned")
        if context.peer_role in self._slots:
            raise CurrentRoleError("authenticated role already reserved")
        generation = self._next_generation()
        reservation = _RoleReservation(context.peer_role, connection_id, generation, context, object())
        self._slots[context.peer_role] = reservation
        return reservation

    def bind_challenge(self, reservation: _RoleReservation, challenge_id: str) -> None:
        self._owner()
        if (
            self._current(reservation) is not reservation
            or reservation.identity in self._challenges
            or not _uuid(challenge_id)
        ):
            raise CurrentRoleError("role reservation is not current")
        self._challenges[reservation.identity] = challenge_id

    def promote(self, reservation: _RoleReservation, handshake: AuthenticatedHandshake) -> _AuthenticatedRoleLease:
        self._owner()
        expected_challenge = self._challenges.pop(reservation.identity, None)
        if (
            self._current(reservation) is not reservation
            or not isinstance(handshake, AuthenticatedHandshake)
            or handshake.context is not reservation.context
            or handshake.connection_id != reservation.connection_id
            or handshake.challenge_id != expected_challenge
            or not handshake.has_full_provenance
        ):
            raise CurrentRoleError("authenticated role promotion rejected")
        lease = _AuthenticatedRoleLease(
            reservation.role,
            reservation.connection_id,
            reservation.generation,
            handshake,
            object(),
        )
        self._slots[reservation.role] = lease
        return lease

    def withdraw(self, token: _RoleReservation | _AuthenticatedRoleLease) -> RoleWithdrawal | None:
        self._owner()
        current = self._current(token)
        if current is not token:
            return None
        self._slots.pop(token.role, None)
        if isinstance(token, _RoleReservation):
            self._challenges.pop(token.identity, None)
        return RoleWithdrawal(token.role, token.connection_id, token.generation)

    def active_roles(self) -> tuple[str, ...]:
        self._owner()
        return tuple(sorted(role for role, token in self._slots.items() if isinstance(token, _AuthenticatedRoleLease)))

    def close(self) -> tuple[RoleWithdrawal, ...]:
        self._owner()
        if self._closed:
            return ()
        self._closed = True
        withdrawals = tuple(
            RoleWithdrawal(token.role, token.connection_id, token.generation) for token in self._slots.values()
        )
        self._slots.clear()
        self._challenges.clear()
        return withdrawals

    def _current(self, token: object):
        role = getattr(token, "role", None)
        return self._slots.get(role)

    def _next_generation(self) -> int:
        if self._generation >= _I63_MAX:
            self._closed = True
            self._slots.clear()
            self._challenges.clear()
            raise CurrentRoleError("role generation exhausted")
        self._generation += 1
        return self._generation

    def _owner(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as error:
            raise CurrentRoleError("role owner requires a running event loop") from error
        thread = threading.get_ident()
        if self._loop is None:
            self._loop, self._thread = loop, thread
        elif self._loop is not loop or self._thread != thread:
            raise CurrentRoleError("wrong role owner loop or thread")


def _uuid(value: object) -> bool:
    return type(value) is str and _UUID.fullmatch(value) is not None
