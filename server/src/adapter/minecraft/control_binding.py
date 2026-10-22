"""Offline coordinator for authenticated Minecraft control binding.

This module owns no socket and performs no authentication. It consumes immutable
results from ``control_auth`` plus server-produced body/ready/active evidence.
All methods are intended to run serially on one event loop.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol
from uuid import uuid4

from src.adapter.minecraft.control_auth import AuthenticatedHandshake
from src.adapter.minecraft.epoch_store import (
    CommittedEpoch,
    EpochCandidate,
    VerifiedReadyContext,
)
from src.adapter.minecraft.typed_ingress import (
    MinecraftObservationBinding,
    TypedMinecraftObservationIngress,
)

_SERVER = "SERVER_AUTHORITY"
_CLIENT = "CLIENT_CONTROLLER"
_MAX_PENDING = 8
_LIFETIME_SECONDS = 5.0
_REVOKE_CLEANUP_SECONDS = 0.1


class ControlBindingError(RuntimeError):
    """Fail-closed coordinator rejection without transport or credential detail."""


class BindingPhase(str, Enum):
    PREPARE = "PREPARE"
    COMMIT = "COMMIT"
    ACTIVE = "ACTIVE"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthenticatedRoleConnection:
    """Authentication result attached to its actual trusted transport connection."""

    connection_id: str
    handshake: AuthenticatedHandshake

    def __post_init__(self) -> None:
        _nonblank(self.connection_id, "connection id")
        if not isinstance(self.handshake, AuthenticatedHandshake):
            raise ValueError("invalid authenticated handshake")

    @property
    def role(self) -> str:
        return self.handshake.context.peer_role

    @property
    def install_id(self) -> str:
        return self.handshake.context.peer_install_id


@dataclass(frozen=True, slots=True, kw_only=True)
class ControlBindingPolicy:
    actor_id: str
    world_id: str
    body_id: str
    character_id: str
    provision_id: str
    server_install_id: str
    client_install_id: str

    def __post_init__(self) -> None:
        for value in (
            self.actor_id,
            self.world_id,
            self.body_id,
            self.character_id,
            self.provision_id,
            self.server_install_id,
            self.client_install_id,
        ):
            _nonblank(value, "binding policy identity")


@dataclass(frozen=True, slots=True, kw_only=True)
class VerifiedBodyBound:
    """Server-origin evidence that proof+notice matched the live game connection."""

    server_boot_id: str
    game_connection_id: str
    body_id: str
    client_install_id: str
    controller_connection_id: str
    evidence_id: str

    def __post_init__(self) -> None:
        for value in (
            self.server_boot_id,
            self.game_connection_id,
            self.body_id,
            self.client_install_id,
            self.controller_connection_id,
            self.evidence_id,
        ):
            _nonblank(value, "body-bound evidence")


@dataclass(frozen=True, slots=True, kw_only=True)
class BindingTuple:
    actor_id: str
    world_id: str
    body_id: str
    session_id: str
    session_epoch: int
    server_boot_id: str
    game_connection_id: str

    def __post_init__(self) -> None:
        for value in (
            self.actor_id,
            self.world_id,
            self.body_id,
            self.session_id,
            self.server_boot_id,
            self.game_connection_id,
        ):
            _nonblank(value, "binding tuple")
        if type(self.session_epoch) is not int or self.session_epoch < 0:
            raise ValueError("invalid binding epoch")


@dataclass(frozen=True, slots=True, kw_only=True)
class BindingPrepare:
    binding: BindingTuple


@dataclass(frozen=True, slots=True, kw_only=True)
class BindingCommit:
    binding: BindingTuple


@dataclass(frozen=True, slots=True, kw_only=True)
class BindingRevoke:
    binding: BindingTuple
    reason: str

    def __post_init__(self) -> None:
        if self.reason not in {"CONTROL_LOST", "QUARANTINE", "REBIND"}:
            raise ValueError("invalid revoke reason")


@dataclass(frozen=True, slots=True, kw_only=True)
class VerifiedBindingReady:
    """Server-origin READY whose quiescence proof was verified before construction."""

    binding: BindingTuple
    evidence_id: str

    def __post_init__(self) -> None:
        _nonblank(self.evidence_id, "ready evidence")


@dataclass(frozen=True, slots=True, kw_only=True)
class VerifiedBindingActive:
    """Server-origin ACTIVE for the exact durable candidate."""

    binding: BindingTuple
    evidence_id: str

    def __post_init__(self) -> None:
        _nonblank(self.evidence_id, "active evidence")


class EpochStorePort(Protocol):
    def read_candidate(self) -> EpochCandidate: ...

    def commit_ready(self, candidate: EpochCandidate, ready: VerifiedReadyContext) -> CommittedEpoch: ...


class ControlCommandPort(Protocol):
    async def send(self, command: BindingPrepare | BindingCommit) -> None: ...

    async def revoke(self, command: BindingRevoke) -> None: ...


class ObservationStagePort(Protocol):
    character_id: str
    world_id: str

    @property
    def minecraft_observation_scope(self): ...

    def bind_minecraft_observations(self, scope) -> None: ...

    def unbind_minecraft_observations(self, scope) -> bool: ...

    async def submit_minecraft_observation(self, fact, scope) -> bool: ...


@dataclass(slots=True)
class _PendingBinding:
    binding: BindingTuple
    candidate: EpochCandidate
    body_bound: VerifiedBodyBound
    server_connection_id: str
    controller_connection_id: str
    deadline: float
    phase: BindingPhase = BindingPhase.PREPARE
    committed: CommittedEpoch | None = None


@dataclass(frozen=True, slots=True)
class ActiveBinding:
    binding: BindingTuple
    server_connection_id: str
    controller_connection_id: str


class MinecraftControlBindingCoordinator:
    """Single-event-loop owner of PREPARE→READY→COMMIT→ACTIVE coordination."""

    def __init__(
        self,
        *,
        policy: ControlBindingPolicy,
        epoch_store: EpochStorePort | None,
        commands: ControlCommandPort,
        stage: ObservationStagePort,
        monotonic_now: Callable[[], float],
        utc_now: Callable,
    ) -> None:
        if stage.character_id != policy.character_id or stage.world_id != policy.world_id:
            raise ValueError("binding policy does not match stage")
        if not callable(monotonic_now) or not callable(utc_now):
            raise TypeError("binding clocks must be callable")
        self._policy = policy
        self._store = epoch_store
        self._commands = commands
        self._stage = stage
        self._monotonic_now = monotonic_now
        self._utc_now = utc_now
        self._roles: dict[str, AuthenticatedRoleConnection] = {}
        self._pending: dict[str, _PendingBinding] = {}
        self._active: ActiveBinding | None = None
        self._ingress: TypedMinecraftObservationIngress | None = None
        self._last_monotonic: float | None = None
        self._quarantined = False

    @property
    def active(self) -> ActiveBinding | None:
        return self._active

    @property
    def ingress(self) -> TypedMinecraftObservationIngress | None:
        return self._ingress

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def quarantined(self) -> bool:
        return self._quarantined

    async def register_authenticated(self, connection: AuthenticatedRoleConnection) -> None:
        expected = {_SERVER: self._policy.server_install_id, _CLIENT: self._policy.client_install_id}
        if connection.role not in expected or connection.install_id != expected[connection.role]:
            raise ControlBindingError("authenticated role is not allowlisted")
        previous = self._roles.get(connection.role)
        if previous is not None and previous.connection_id != connection.connection_id:
            await self._fence("REBIND")
        self._roles[connection.role] = connection

    async def disconnect(self, connection_id: str) -> None:
        removed = False
        for role, connection in tuple(self._roles.items()):
            if connection.connection_id == connection_id:
                self._roles.pop(role, None)
                removed = True
        if removed:
            await self._fence("CONTROL_LOST")

    async def prepare(
        self,
        server: AuthenticatedRoleConnection,
        body_bound: VerifiedBodyBound,
    ) -> BindingPrepare:
        now = await self._begin_operation()
        self._require_current(server, _SERVER)
        controller = self._roles.get(_CLIENT)
        if (
            controller is None
            or body_bound.body_id != self._policy.body_id
            or body_bound.client_install_id != self._policy.client_install_id
            or body_bound.controller_connection_id != controller.connection_id
        ):
            raise ControlBindingError("body-bound evidence does not match current controller")
        for pending in self._pending.values():
            if (
                pending.phase is BindingPhase.PREPARE
                and pending.body_bound == body_bound
                and pending.server_connection_id == server.connection_id
                and pending.controller_connection_id == controller.connection_id
            ):
                return BindingPrepare(binding=pending.binding)
        if self._quarantined or self._store is None:
            raise ControlBindingError("control activation is disabled")
        if len(self._pending) >= _MAX_PENDING:
            raise ControlBindingError("binding pending capacity")
        try:
            candidate = self._store.read_candidate()
        except Exception as error:
            self._quarantined = True
            raise ControlBindingError("epoch candidate is unavailable") from error
        if candidate.body_id != self._policy.body_id or candidate.provision_id != self._policy.provision_id:
            self._quarantined = True
            raise ControlBindingError("epoch candidate identity mismatch")
        binding = BindingTuple(
            actor_id=self._policy.actor_id,
            world_id=self._policy.world_id,
            body_id=self._policy.body_id,
            session_id=str(uuid4()),
            session_epoch=candidate.next_epoch,
            server_boot_id=body_bound.server_boot_id,
            game_connection_id=body_bound.game_connection_id,
        )
        pending = _PendingBinding(
            binding=binding,
            candidate=candidate,
            body_bound=body_bound,
            server_connection_id=server.connection_id,
            controller_connection_id=controller.connection_id,
            deadline=now + _LIFETIME_SECONDS,
        )
        self._pending[binding.session_id] = pending
        command = BindingPrepare(binding=binding)
        try:
            await self._commands.send(command)
        except asyncio.CancelledError as cancellation:
            await self._cancelled_fence("QUARANTINE", cancellation)
        except Exception as error:
            self._pending.pop(binding.session_id, None)
            await self._fence("QUARANTINE")
            raise ControlBindingError("prepare delivery is uncertain") from error
        return command

    async def ready(
        self,
        server: AuthenticatedRoleConnection,
        ready: VerifiedBindingReady,
    ) -> BindingCommit:
        await self._begin_operation()
        pending = self._pending.get(ready.binding.session_id)
        if pending is not None and pending.binding == ready.binding and pending.phase is BindingPhase.COMMIT:
            self._require_current(server, _SERVER)
            return BindingCommit(binding=pending.binding)
        pending = self._matching_pending(server, ready.binding, BindingPhase.PREPARE)
        try:
            committed = self._store.commit_ready(
                pending.candidate,
                VerifiedReadyContext(
                    provision_id=pending.candidate.provision_id,
                    body_id=pending.candidate.body_id,
                    expected_last_epoch=pending.candidate.expected_last_epoch,
                    expected_generation=pending.candidate.expected_generation,
                    next_epoch=pending.candidate.next_epoch,
                    evidence_id=ready.evidence_id,
                ),
            )
        except Exception as error:
            self._pending.pop(pending.binding.session_id, None)
            await self._fence("QUARANTINE")
            raise ControlBindingError("epoch commit failed") from error
        pending.phase = BindingPhase.COMMIT
        pending.committed = committed
        command = BindingCommit(binding=pending.binding)
        try:
            await self._commands.send(command)
        except asyncio.CancelledError as cancellation:
            await self._cancelled_fence("QUARANTINE", cancellation)
        except Exception as error:
            await self._fence("QUARANTINE")
            raise ControlBindingError("commit delivery is uncertain") from error
        return command

    async def activate(
        self,
        server: AuthenticatedRoleConnection,
        active: VerifiedBindingActive,
    ) -> TypedMinecraftObservationIngress:
        await self._begin_operation()
        if self._active is not None and self._active.binding == active.binding:
            self._require_current(server, _SERVER)
            if self._ingress is None:
                raise ControlBindingError("active ingress is unavailable")
            return self._ingress
        pending = self._matching_pending(server, active.binding, BindingPhase.COMMIT)
        controller = self._roles.get(_CLIENT)
        if (
            pending.committed is None
            or pending.committed.last_epoch != pending.binding.session_epoch
            or controller is None
            or controller.connection_id != pending.controller_connection_id
        ):
            await self._fence("QUARANTINE")
            raise ControlBindingError("active evidence does not match durable candidate")
        observation = MinecraftObservationBinding(
            actor_id=pending.binding.actor_id,
            world_id=pending.binding.world_id,
            body_id=pending.binding.body_id,
            session_id=pending.binding.session_id,
            session_epoch=pending.binding.session_epoch,
            server_boot_id=pending.binding.server_boot_id,
            character_id=self._policy.character_id,
        )
        self._stage.bind_minecraft_observations(observation.observation_scope)
        ingress = TypedMinecraftObservationIngress(binding=observation, target=self._stage, now=self._utc_now)
        self._active = ActiveBinding(
            pending.binding,
            pending.server_connection_id,
            pending.controller_connection_id,
        )
        pending.phase = BindingPhase.ACTIVE
        self._pending.pop(pending.binding.session_id, None)
        self._ingress = ingress
        self._quarantined = False
        return ingress

    async def check_timeouts(self) -> None:
        now = await self._begin_operation(prune=False)
        if any(now >= pending.deadline for pending in self._pending.values()):
            await self._fence("QUARANTINE")

    async def _begin_operation(self, *, prune: bool = True) -> float:
        try:
            now = self._monotonic_now()
        except Exception as error:
            await self._fence("QUARANTINE")
            raise ControlBindingError("monotonic clock failed") from error
        if type(now) not in (int, float) or isinstance(now, bool) or not math.isfinite(now) or now < 0:
            await self._fence("QUARANTINE")
            raise ControlBindingError("invalid monotonic clock")
        now = float(now)
        if self._last_monotonic is not None and now < self._last_monotonic:
            await self._fence("QUARANTINE")
            raise ControlBindingError("monotonic clock rolled back")
        self._last_monotonic = now
        if prune and any(now >= pending.deadline for pending in self._pending.values()):
            await self._fence("QUARANTINE")
            raise ControlBindingError("binding handshake expired")
        return now

    def _matching_pending(
        self,
        server: AuthenticatedRoleConnection,
        binding: BindingTuple,
        phase: BindingPhase,
    ) -> _PendingBinding:
        self._require_current(server, _SERVER)
        pending = self._pending.get(binding.session_id)
        if (
            pending is None
            or pending.binding != binding
            or pending.phase is not phase
            or pending.server_connection_id != server.connection_id
        ):
            raise ControlBindingError("binding evidence does not match pending state")
        return pending

    def _require_current(self, connection: AuthenticatedRoleConnection, role: str) -> None:
        if connection.role != role or self._roles.get(role) != connection:
            raise ControlBindingError("authenticated origin is not current")

    async def _fence(self, reason: str) -> None:
        command = self._fence_local(reason)
        await self._best_effort_revoke(command)

    async def _cancelled_fence(self, reason: str, cancellation: asyncio.CancelledError) -> None:
        command = self._fence_local(reason)
        await self._best_effort_revoke(command)
        raise cancellation

    def _fence_local(self, reason: str) -> BindingRevoke | None:
        """Synchronously remove all local authority before any cleanup await."""
        binding = self._active.binding if self._active is not None else None
        if binding is None and self._pending:
            binding = next(iter(self._pending.values())).binding
        if self._active is not None:
            observation = MinecraftObservationBinding(
                actor_id=self._active.binding.actor_id,
                world_id=self._active.binding.world_id,
                body_id=self._active.binding.body_id,
                session_id=self._active.binding.session_id,
                session_epoch=self._active.binding.session_epoch,
                server_boot_id=self._active.binding.server_boot_id,
                character_id=self._policy.character_id,
            )
            self._stage.unbind_minecraft_observations(observation.observation_scope)
        self._active = None
        self._ingress = None
        self._pending.clear()
        self._quarantined = True
        return None if binding is None else BindingRevoke(binding=binding, reason=reason)

    async def _best_effort_revoke(self, command: BindingRevoke | None) -> None:
        if command is None:
            return
        try:
            await asyncio.wait_for(self._commands.revoke(command), timeout=_REVOKE_CLEANUP_SECONDS)
        except BaseException:
            # Local authority was already removed. Notification is neither a
            # quiescence proof nor allowed to delay fail-closed cancellation.
            return


def _nonblank(value: object, description: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid {description}")
