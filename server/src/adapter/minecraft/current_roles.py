"""Single-owner current authenticated role reservations for Minecraft control."""

from __future__ import annotations

import asyncio
import re
import threading
from dataclasses import dataclass
from typing import Callable

from .control_auth import AuthenticatedHandshake, ControlAuthContext

_ROLES = frozenset(("SERVER_AUTHORITY", "CLIENT_CONTROLLER"))
_I63_MAX = (1 << 63) - 1
_UUID = re.compile(
    r"(?!00000000-0000-0000-0000-000000000000$)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


class CurrentRoleError(RuntimeError):
    """Fail-closed role ownership error without authentication material."""


class RoleWithdrawal:
    """Opaque owner-issued proof that one exact authenticated lease was removed."""

    __slots__ = ("role", "connection_id", "generation")

    def __init__(self, role: str, connection_id: str, generation: int) -> None:
        self.role = role
        self.connection_id = connection_id
        self.generation = generation

    def __repr__(self) -> str:
        return "RoleWithdrawal(<opaque>)"


@dataclass(frozen=True, slots=True, repr=False)
class _RoleReservation:
    role: str
    connection_id: str
    generation: int
    context: ControlAuthContext
    identity: object


@dataclass(frozen=True, slots=True, repr=False)
class AuthenticatedRoleLease:
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
        self._slots: dict[str, _RoleReservation | AuthenticatedRoleLease] = {}
        self._challenges: dict[object, str] = {}
        self._generation = 0
        self._closed = False
        self._thread: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._withdrawals: dict[str, tuple[RoleWithdrawal, AuthenticatedRoleLease]] = {}

    def reserve(self, context: ControlAuthContext, connection_id: str) -> _RoleReservation:
        self._owner()
        if self._closed or self._expected.get(context.peer_role) is not context or not _uuid(connection_id):
            raise CurrentRoleError("role context is not provisioned")
        if context.peer_role in self._slots:
            raise CurrentRoleError("authenticated role already reserved")
        self._withdrawals.pop(context.peer_role, None)
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

    def promote(self, reservation: _RoleReservation, handshake: AuthenticatedHandshake) -> AuthenticatedRoleLease:
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
        lease = AuthenticatedRoleLease(
            reservation.role,
            reservation.connection_id,
            reservation.generation,
            handshake,
            object(),
        )
        self._slots[reservation.role] = lease
        return lease

    def current(self, role: str) -> AuthenticatedRoleLease | None:
        self._owner()
        if role not in _ROLES:
            raise CurrentRoleError("invalid authenticated role")
        token = self._slots.get(role)
        return token if isinstance(token, AuthenticatedRoleLease) else None

    def is_current(self, lease: AuthenticatedRoleLease) -> bool:
        self._owner()
        return isinstance(lease, AuthenticatedRoleLease) and self._current(lease) is lease

    def assert_owner(self) -> None:
        self._owner()

    def consume_withdrawal(self, withdrawal: RoleWithdrawal, lease: AuthenticatedRoleLease) -> bool:
        self._owner()
        if not isinstance(withdrawal, RoleWithdrawal) or not isinstance(lease, AuthenticatedRoleLease):
            return False
        issued = self._withdrawals.get(lease.role)
        if (
            issued is None
            or issued[0] is not withdrawal
            or issued[1] is not lease
            or withdrawal.role != lease.role
            or withdrawal.connection_id != lease.connection_id
            or withdrawal.generation != lease.generation
        ):
            return False
        self._withdrawals.pop(lease.role, None)
        return True

    def matches_contexts(self, contexts: tuple[ControlAuthContext, ...], hub_install_id: str) -> bool:
        if type(contexts) is not tuple or type(hub_install_id) is not str:
            return False
        supplied = {context.peer_role: context for context in contexts if isinstance(context, ControlAuthContext)}
        return (
            len(supplied) == len(contexts) == len(self._expected)
            and all(self._expected.get(role) is context for role, context in supplied.items())
            and all(context.hub_install_id == hub_install_id for context in supplied.values())
        )

    def matches_role_installs(self, expected: dict[str, str]) -> bool:
        return (
            type(expected) is dict
            and set(expected) == set(self._expected)
            and all(
                type(install_id) is str and self._expected[role].peer_install_id == install_id
                for role, install_id in expected.items()
            )
        )

    def withdraw(
        self,
        token: _RoleReservation | AuthenticatedRoleLease,
        after_remove: Callable[[RoleWithdrawal], None] | None = None,
    ) -> RoleWithdrawal | None:
        self._owner()
        if after_remove is not None and not callable(after_remove):
            raise TypeError("withdraw callback must be callable")
        current = self._current(token)
        if current is not token:
            return None
        self._slots.pop(token.role, None)
        if isinstance(token, _RoleReservation):
            self._challenges.pop(token.identity, None)
        withdrawal = None
        if isinstance(token, AuthenticatedRoleLease):
            withdrawal = RoleWithdrawal(token.role, token.connection_id, token.generation)
            self._withdrawals[token.role] = (withdrawal, token)
        if after_remove is not None:
            if withdrawal is not None:
                after_remove(withdrawal)
        return withdrawal

    def active_roles(self) -> tuple[str, ...]:
        self._owner()
        return tuple(sorted(role for role, token in self._slots.items() if isinstance(token, AuthenticatedRoleLease)))

    def close(self) -> tuple[RoleWithdrawal, ...]:
        self._owner()
        if self._closed:
            return ()
        self._closed = True
        withdrawals = []
        for token in self._slots.values():
            if isinstance(token, AuthenticatedRoleLease):
                withdrawal = RoleWithdrawal(token.role, token.connection_id, token.generation)
                self._withdrawals[token.role] = (withdrawal, token)
                withdrawals.append(withdrawal)
        self._slots.clear()
        self._challenges.clear()
        return tuple(withdrawals)

    def _current(self, token: object):
        role = getattr(token, "role", None)
        return self._slots.get(role)

    def _next_generation(self) -> int:
        if self._generation >= _I63_MAX:
            self._closed = True
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
