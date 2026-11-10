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
from src.adapter.minecraft.current_roles import (
    AuthenticatedControlRoleOwner,
    AuthenticatedRoleLease,
    RoleWithdrawal,
)
from src.adapter.minecraft.epoch_store import (
    CommittedEpoch,
    EpochCandidate,
    VerifiedReadyContext,
)
from src.adapter.minecraft.epoch_worker import (
    EpochCommitRequest,
    EpochJobHandle,
    EpochPreflightRequest,
    EpochPreflightSnapshot,
    EpochWorkerError,
    EpochWorkerState,
    MinecraftEpochStoreWorker,
)
from src.adapter.minecraft.typed_ingress import (
    MinecraftObservationBinding,
    TypedMinecraftObservationIngress,
)
from src.adapter.minecraft.v2_body_routes import (
    BodyRouteAdmissionReceipt,
    BodyRoutePreflightContext,
    V2BodyRouteOwner,
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


class V2DispatchState(str, Enum):
    UNDISPATCHED = "UNDISPATCHED"
    DISPATCHING = "DISPATCHING"
    DELIVERED_CURRENT = "DELIVERED_CURRENT"
    STALE_OR_UNKNOWN = "STALE_OR_UNKNOWN"


class V2PrepareDispatch:
    __slots__ = ("_identity", "_command", "_frozen")

    def __init__(self, identity: object | None = None, command: BindingPrepare | None = None) -> None:
        object.__setattr__(self, "_identity", identity)
        object.__setattr__(self, "_command", command)
        object.__setattr__(self, "_frozen", True)

    @property
    def command(self) -> BindingPrepare | None:
        return self._command

    def __setattr__(self, name, value):
        if getattr(self, "_frozen", False):
            raise AttributeError("V2PrepareDispatch is immutable")
        object.__setattr__(self, name, value)

    def __repr__(self) -> str:
        return "V2PrepareDispatch(<opaque>)"

    def __copy__(self):
        return V2PrepareDispatch()

    def __deepcopy__(self, memo):
        return V2PrepareDispatch()


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
    server_lease: AuthenticatedRoleLease
    controller_lease: AuthenticatedRoleLease
    deadline: float
    phase: BindingPhase = BindingPhase.PREPARE
    committed: CommittedEpoch | None = None
    v2: bool = False
    preflight: EpochPreflightSnapshot | None = None
    preflight_handle: EpochJobHandle | None = None
    commit_io_pending: bool = False
    route_receipt: BodyRouteAdmissionReceipt | None = None
    route_context: BodyRoutePreflightContext | None = None


@dataclass(slots=True)
class _V2Ticket:
    pending: _PendingBinding
    generation: int
    server_lease: AuthenticatedRoleLease
    controller_lease: AuthenticatedRoleLease
    session_id: str
    route_id: str
    state: V2DispatchState = V2DispatchState.UNDISPATCHED
    ready: VerifiedBindingReady | None = None
    dispatch: V2PrepareDispatch | None = None
    command: BindingPrepare | None = None
    commit: BindingCommit | None = None


@dataclass(frozen=True, slots=True)
class ActiveBinding:
    binding: BindingTuple
    server_lease: AuthenticatedRoleLease
    controller_lease: AuthenticatedRoleLease

    @property
    def server_connection_id(self) -> str:
        return self.server_lease.connection_id

    @property
    def controller_connection_id(self) -> str:
        return self.controller_lease.connection_id


@dataclass(frozen=True, slots=True)
class ActiveBindingSnapshot:
    """One immutable coordinator generation captured without awaiting."""

    generation: int
    active: ActiveBinding
    server: AuthenticatedRoleConnection


class MinecraftControlBindingCoordinator:
    """Single-event-loop owner of PREPARE→READY→COMMIT→ACTIVE coordination."""

    def __init__(
        self,
        *,
        policy: ControlBindingPolicy,
        role_owner: AuthenticatedControlRoleOwner,
        epoch_store: EpochStorePort | None,
        epoch_worker: MinecraftEpochStoreWorker | None = None,
        commands: ControlCommandPort,
        stage: ObservationStagePort,
        monotonic_now: Callable[[], float],
        utc_now: Callable,
    ) -> None:
        if not isinstance(role_owner, AuthenticatedControlRoleOwner):
            raise TypeError("binding requires authenticated role owner")
        if not role_owner.matches_role_installs({_SERVER: policy.server_install_id, _CLIENT: policy.client_install_id}):
            raise ValueError("binding policy does not match role owner")
        if stage.character_id != policy.character_id or stage.world_id != policy.world_id:
            raise ValueError("binding policy does not match stage")
        if not callable(monotonic_now) or not callable(utc_now):
            raise TypeError("binding clocks must be callable")
        self._policy = policy
        self._role_owner = role_owner
        self._store = epoch_store
        self._epoch_worker = epoch_worker
        self._commands = commands
        self._stage = stage
        self._monotonic_now = monotonic_now
        self._utc_now = utc_now
        self._pending: dict[str, _PendingBinding] = {}
        self._active: ActiveBinding | None = None
        self._ingress: TypedMinecraftObservationIngress | None = None
        self._last_monotonic: float | None = None
        self._quarantined = False
        self._generation = 0
        self._v2_tickets: dict[str, _V2Ticket] = {}

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

    @property
    def role_owner(self) -> AuthenticatedControlRoleOwner:
        return self._role_owner

    @property
    def epoch_worker(self) -> MinecraftEpochStoreWorker | None:
        return self._epoch_worker

    def active_snapshot(self) -> ActiveBindingSnapshot | None:
        """Return one coherent current ACTIVE/server-origin snapshot."""
        active = self._active
        if (
            active is None
            or self._quarantined
            or not self._role_owner.is_current(active.server_lease)
            or not self._role_owner.is_current(active.controller_lease)
        ):
            return None
        return ActiveBindingSnapshot(
            self._generation,
            active,
            AuthenticatedRoleConnection(
                connection_id=active.server_lease.connection_id,
                handshake=active.server_lease.handshake,
            ),
        )

    def v2_admission_available(self) -> bool:
        self._role_owner.assert_owner()
        return (
            self._epoch_worker is not None
            and self._epoch_worker.state is EpochWorkerState.AVAILABLE
            and not self._quarantined
        )

    async def prepare_v2(
        self,
        routes: V2BodyRouteOwner,
        receipt: BodyRouteAdmissionReceipt,
    ) -> V2PrepareDispatch:
        self._role_owner.assert_owner()
        handle = snapshot = context = None
        snapshot = None
        try:
            await self._begin_operation()
            if self._epoch_worker is None or self._epoch_worker.state is not EpochWorkerState.AVAILABLE:
                raise ControlBindingError("v2 control activation is disabled")
            context = routes.inspect_for_preflight(receipt)
            if (
                context.candidate.body_id != self._policy.body_id
                or context.server_lease.handshake.context.peer_install_id != self._policy.server_install_id
                or context.controller_lease.handshake.context.peer_install_id != self._policy.client_install_id
            ):
                raise ControlBindingError("v2 route does not match policy")
            generation = self._generation
            active = self._active
            pending_before = tuple(self._pending.values())
            handle = self._epoch_worker.submit_preflight(
                EpochPreflightRequest(
                    provision_id=self._policy.provision_id,
                    body_id=self._policy.body_id,
                    coordinator_generation=generation,
                    route_id=context.route_id,
                    server_connection_id=context.server_lease.connection_id,
                    controller_connection_id=context.controller_lease.connection_id,
                )
            )
            await self._epoch_worker.wait(handle)
            snapshot = self._epoch_worker.take_preflight(handle)
            session_id = str(uuid4())
            return self._admit_v2_prepare(
                routes, receipt, context, handle, snapshot, generation, active, pending_before, session_id
            )
        except asyncio.CancelledError:
            self._obsolete_preflight(handle, snapshot)
            routes.invalidate_admission(receipt)
            raise
        except Exception:
            self._obsolete_preflight(handle, snapshot)
            routes.invalidate_admission(receipt)
            raise ControlBindingError("v2 preflight admission failed") from None

    def _admit_v2_prepare(
        self,
        routes,
        receipt,
        context,
        handle,
        snapshot,
        generation,
        active,
        pending_before,
        session_id,
    ) -> V2PrepareDispatch:
        redemption = routes._prevalidate_redemption(receipt, context)
        actual_pending = tuple(self._pending.values())
        valid = (
            self._generation == generation
            and self._active is active
            and len(actual_pending) == len(pending_before)
            and all(a is b for a, b in zip(actual_pending, pending_before))
            and not self._quarantined
            and len(self._pending) < _MAX_PENDING
            and self._leases_current(context.server_lease, context.controller_lease)
            and self._epoch_worker.validates_preflight(
                snapshot,
                provision_id=self._policy.provision_id,
                body_id=self._policy.body_id,
                coordinator_generation=generation,
                route_id=context.route_id,
                server_connection_id=context.server_lease.connection_id,
                controller_connection_id=context.controller_lease.connection_id,
            )
        )
        if not valid:
            raise ControlBindingError("v2 preflight is stale")
        candidate = routes._redeem_prevalidated(receipt, redemption)
        binding = BindingTuple(
            actor_id=self._policy.actor_id,
            world_id=self._policy.world_id,
            body_id=self._policy.body_id,
            session_id=session_id,
            session_epoch=snapshot.next_epoch,
            server_boot_id=candidate.server_boot_id,
            game_connection_id=candidate.game_connection_id,
        )
        pending = _PendingBinding(
            binding=binding,
            candidate=EpochCandidate(
                snapshot.provision_id,
                snapshot.body_id,
                snapshot.expected_last_epoch,
                snapshot.expected_generation,
                snapshot.next_epoch,
            ),
            body_bound=VerifiedBodyBound(
                server_boot_id=candidate.server_boot_id,
                game_connection_id=candidate.game_connection_id,
                body_id=candidate.body_id,
                client_install_id=context.controller_lease.handshake.context.peer_install_id,
                controller_connection_id=context.controller_lease.connection_id,
                evidence_id=context.route_id,
            ),
            server_lease=context.server_lease,
            controller_lease=context.controller_lease,
            deadline=self._last_monotonic + _LIFETIME_SECONDS,
            v2=True,
            preflight=snapshot,
            preflight_handle=handle,
            route_receipt=receipt,
            route_context=context,
        )
        ticket = _V2Ticket(
            pending,
            generation,
            context.server_lease,
            context.controller_lease,
            session_id,
            context.route_id,
        )
        command = BindingPrepare(binding=binding)
        dispatch = V2PrepareDispatch(object(), command)
        ticket.dispatch = dispatch
        ticket.command = command
        self._pending[session_id] = pending
        self._v2_tickets[session_id] = ticket
        return dispatch

    async def dispatch_v2_prepare(self, dispatch: V2PrepareDispatch) -> BindingPrepare:
        self._role_owner.assert_owner()
        if type(dispatch) is not V2PrepareDispatch:
            raise ControlBindingError("v2 dispatch ticket is not current")
        ticket = next((value for value in self._v2_tickets.values() if value.dispatch is dispatch), None)
        if ticket is None or ticket.state is not V2DispatchState.UNDISPATCHED or not self._v2_ticket_current(ticket):
            raise ControlBindingError("v2 dispatch ticket is not current")
        ticket.state = V2DispatchState.DISPATCHING
        command = ticket.command
        if command is None or command.binding is not ticket.pending.binding:
            raise ControlBindingError("v2 dispatch ticket is not current")
        try:
            await self._commands.send(command)
        except asyncio.CancelledError as error:
            await self._fail_v2_ticket(ticket, error)
        except Exception as error:
            await self._fail_v2_ticket(ticket, ControlBindingError("v2 prepare delivery is uncertain"))
            raise ControlBindingError("v2 prepare delivery is uncertain") from error
        if not self._v2_ticket_current(ticket):
            await self._fail_v2_ticket(ticket, ControlBindingError("v2 prepare delivery is stale"))
        ticket.state = V2DispatchState.DELIVERED_CURRENT
        if ticket.ready is not None:
            await self._settle_v2_ready(ticket)
        return command

    def receive_v2_ready(self, server: AuthenticatedRoleConnection, ready: VerifiedBindingReady) -> None:
        self._require_current(server, _SERVER)
        ticket = self._v2_tickets.get(ready.binding.session_id)
        if ticket is None or not self._v2_ticket_current(ticket):
            raise ControlBindingError("v2 READY does not match current ticket")
        if ticket.pending.binding != ready.binding:
            ticket.state = V2DispatchState.STALE_OR_UNKNOWN
            ticket.ready = None
            self._fence_local("QUARANTINE")
            raise ControlBindingError("conflicting v2 READY")
        if ticket.state is V2DispatchState.UNDISPATCHED:
            self._fence_local("QUARANTINE")
            raise ControlBindingError("v2 READY arrived before dispatch")
        if ticket.state not in {V2DispatchState.DISPATCHING, V2DispatchState.DELIVERED_CURRENT}:
            raise ControlBindingError("v2 READY ticket is stale")
        if ticket.ready is not None:
            self._fence_local("QUARANTINE")
            raise ControlBindingError("duplicate v2 READY")
        ticket.ready = ready

    async def admit_v2_ready(
        self,
        server: AuthenticatedRoleConnection,
        ready: VerifiedBindingReady,
    ) -> BindingCommit | None:
        """Record a READY and settle it once its ticket is already delivered."""
        self._role_owner.assert_owner()
        self.receive_v2_ready(server, ready)
        ticket = self._v2_tickets.get(ready.binding.session_id)
        if ticket is None or ticket.ready is not ready:
            return None
        if ticket.state is V2DispatchState.DELIVERED_CURRENT:
            return await self._settle_v2_ready(ticket)
        return None

    async def _settle_v2_ready(self, ticket: _V2Ticket) -> BindingCommit:  # noqa: C901
        """Run the store-owner READY CAS, then dispatch COMMIT once."""
        self._role_owner.assert_owner()
        pending = ticket.pending
        ready = ticket.ready
        if ready is None or ticket.state is not V2DispatchState.DELIVERED_CURRENT:
            raise ControlBindingError("v2 READY is not dispatchable")
        if ticket.commit is not None:
            return ticket.commit
        if pending.phase is not BindingPhase.PREPARE or not self._v2_ticket_current(ticket):
            await self._fence_v2_ticket(ticket, "v2 READY is stale")
        worker = self._epoch_worker
        snapshot = pending.preflight
        if worker is None or snapshot is None:
            raise ControlBindingError("v2 control activation is disabled")
        if not worker.validates_preflight(
            snapshot,
            provision_id=self._policy.provision_id,
            body_id=self._policy.body_id,
            coordinator_generation=ticket.generation,
            route_id=ticket.route_id,
            server_connection_id=ticket.server_lease.connection_id,
            controller_connection_id=ticket.controller_lease.connection_id,
        ):
            await self._fence_v2_ticket(ticket, "v2 preflight is stale")
        pending.commit_io_pending = True
        committed_snapshot = None
        try:
            handle = worker.submit_commit(
                EpochCommitRequest(
                    preflight=snapshot,
                    evidence_id=ready.evidence_id,
                    pending_identity=pending,
                    coordinator_generation=ticket.generation,
                    route_id=ticket.route_id,
                    server_connection_id=ticket.server_lease.connection_id,
                    controller_connection_id=ticket.controller_lease.connection_id,
                )
            )
            await worker.wait(handle)
            committed_snapshot = worker.take_commit(handle)
        except asyncio.CancelledError as cancellation:
            ticket.state = V2DispatchState.STALE_OR_UNKNOWN
            ticket.ready = None
            command = self._fence_local("QUARANTINE")
            await self._best_effort_revoke(command)
            raise cancellation
        except Exception:
            await self._fence_v2_ticket(ticket, "v2 epoch commit failed")
        finally:
            pending.commit_io_pending = False
        if committed_snapshot is None:
            await self._fence_v2_ticket(ticket, "v2 epoch commit failed")
        if not self._v2_ticket_current(ticket) or pending.phase is not BindingPhase.PREPARE:
            await self._fence_v2_ticket(ticket, "v2 epoch commit is stale")
        if (
            committed_snapshot.pending_identity is not pending
            or committed_snapshot.coordinator_generation != ticket.generation
            or committed_snapshot.last_epoch != pending.binding.session_epoch
        ):
            await self._fence_v2_ticket(ticket, "v2 epoch commit mismatch")
        pending.phase = BindingPhase.COMMIT
        pending.committed = CommittedEpoch(
            committed_snapshot.provision_id,
            committed_snapshot.body_id,
            committed_snapshot.last_epoch,
            committed_snapshot.generation,
        )
        command = BindingCommit(binding=pending.binding)
        try:
            await self._commands.send(command)
        except asyncio.CancelledError as cancellation:
            await self._cancelled_pending_fence(pending, "QUARANTINE", cancellation)
        except Exception as error:
            await self._fence_pending(pending, "QUARANTINE")
            raise ControlBindingError("v2 commit delivery is uncertain") from error
        if not self._guard_current(pending, BindingPhase.COMMIT):
            raise ControlBindingError("v2 commit delivery completed after authority changed")
        ticket.commit = command
        return command

    async def _fence_v2_ticket(self, ticket: _V2Ticket, message: str) -> None:
        ticket.state = V2DispatchState.STALE_OR_UNKNOWN
        ticket.ready = None
        if self._v2_ticket_current_owned(ticket):
            command = self._fence_local("QUARANTINE")
            await self._best_effort_revoke(command)
        raise ControlBindingError(message)

    def _v2_ticket_current(self, ticket: _V2Ticket) -> bool:
        return (
            self._v2_tickets.get(ticket.session_id) is ticket
            and self._pending.get(ticket.session_id) is ticket.pending
            and self._generation == ticket.generation
            and self._leases_current(ticket.server_lease, ticket.controller_lease)
            and ticket.state is not V2DispatchState.STALE_OR_UNKNOWN
        )

    async def _fail_v2_ticket(self, ticket: _V2Ticket, error: BaseException) -> None:
        ticket.state = V2DispatchState.STALE_OR_UNKNOWN
        ticket.ready = None
        if self._v2_ticket_current_owned(ticket):
            command = self._fence_local("QUARANTINE")
            await self._best_effort_revoke(command)
        if isinstance(error, asyncio.CancelledError):
            raise error
        raise ControlBindingError("v2 prepare delivery is stale") from None

    def _v2_ticket_current_owned(self, ticket: _V2Ticket) -> bool:
        return (
            self._v2_tickets.get(ticket.session_id) is ticket
            and self._pending.get(ticket.session_id) is ticket.pending
            and self._generation == ticket.generation
        )

    def _obsolete_preflight(self, handle, snapshot) -> None:
        if self._epoch_worker is None or (handle is None and snapshot is None):
            return
        try:
            self._epoch_worker.mark_obsolete(snapshot if snapshot is not None else handle)
        except EpochWorkerError:
            pass

    def fence_role_withdrawal(self, withdrawal: RoleWithdrawal, reason: str = "CONTROL_LOST") -> BindingRevoke | None:
        self._role_owner.assert_owner()
        if not isinstance(withdrawal, RoleWithdrawal) or reason != "CONTROL_LOST":
            raise ControlBindingError("invalid role withdrawal")
        target = self._withdrawal_target(withdrawal)
        if target is None or not self._role_owner.consume_withdrawal(withdrawal, target):
            return None
        return self._fence_local(reason)

    async def notify_revoke(self, command: BindingRevoke | None) -> None:
        await self._best_effort_revoke(command)

    async def prepare(  # noqa: C901
        self,
        server: AuthenticatedRoleConnection,
        body_bound: VerifiedBodyBound,
    ) -> BindingPrepare:
        now = await self._begin_operation()
        server_lease = self._require_current(server, _SERVER)
        controller_lease = self._role_owner.current(_CLIENT)
        controller = self._connection_from_lease(controller_lease)
        operation_generation = self._generation
        operation_active = self._active
        operation_pending = tuple(self._pending.values())
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
                and pending.server_lease is server_lease
                and pending.controller_lease is controller_lease
                and self._guard_current(pending, BindingPhase.PREPARE)
            ):
                return BindingPrepare(binding=pending.binding)
        if self._quarantined or self._store is None:
            raise ControlBindingError("control activation is disabled")
        if len(self._pending) >= _MAX_PENDING:
            raise ControlBindingError("binding pending capacity")
        try:
            candidate = self._store.read_candidate()
        except Exception as error:
            self._fence_prepare_operation(
                operation_generation,
                operation_active,
                operation_pending,
                server_lease,
                controller_lease,
            )
            raise ControlBindingError("epoch candidate is unavailable") from error
        if not self._prepare_operation_current(
            operation_generation,
            operation_active,
            operation_pending,
            server_lease,
            controller_lease,
        ):
            raise ControlBindingError("binding origin changed during candidate read")
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
            server_lease=server_lease,
            controller_lease=controller_lease,
            deadline=now + _LIFETIME_SECONDS,
        )
        self._pending[binding.session_id] = pending
        command = BindingPrepare(binding=binding)
        try:
            await self._commands.send(command)
        except asyncio.CancelledError as cancellation:
            await self._cancelled_pending_fence(pending, "QUARANTINE", cancellation)
        except Exception as error:
            await self._fence_pending(pending, "QUARANTINE")
            raise ControlBindingError("prepare delivery is uncertain") from error
        if not self._guard_current(pending, BindingPhase.PREPARE):
            raise ControlBindingError("prepare delivery completed after authority changed")
        return command

    async def ready(
        self,
        server: AuthenticatedRoleConnection,
        ready: VerifiedBindingReady,
    ) -> BindingCommit:
        await self._begin_operation()
        pending = self._pending.get(ready.binding.session_id)
        if pending is not None and pending.v2:
            raise ControlBindingError("v2 READY requires headless ticket handling")
        if pending is not None and pending.binding == ready.binding and pending.phase is BindingPhase.COMMIT:
            self._require_current(server, _SERVER)
            if not self._guard_current(pending, BindingPhase.COMMIT):
                raise ControlBindingError("commit is no longer current")
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
            await self._fence_pending(pending, "QUARANTINE")
            raise ControlBindingError("epoch commit failed") from error
        if not self._guard_current(pending, BindingPhase.PREPARE):
            raise ControlBindingError("epoch commit completed after authority changed")
        pending.phase = BindingPhase.COMMIT
        pending.committed = committed
        command = BindingCommit(binding=pending.binding)
        try:
            await self._commands.send(command)
        except asyncio.CancelledError as cancellation:
            await self._cancelled_pending_fence(pending, "QUARANTINE", cancellation)
        except Exception as error:
            await self._fence_pending(pending, "QUARANTINE")
            raise ControlBindingError("commit delivery is uncertain") from error
        if not self._guard_current(pending, BindingPhase.COMMIT):
            raise ControlBindingError("commit delivery completed after authority changed")
        return command

    async def activate(
        self,
        server: AuthenticatedRoleConnection,
        active: VerifiedBindingActive,
    ) -> TypedMinecraftObservationIngress:
        await self._begin_operation()
        pending_value = self._pending.get(active.binding.session_id)
        if pending_value is not None and pending_value.v2:
            ticket = self._v2_tickets.get(active.binding.session_id)
            if (
                ticket is None
                or ticket.commit is None
                or pending_value.phase is not BindingPhase.COMMIT
                or not self._v2_ticket_current(ticket)
            ):
                raise ControlBindingError("v2 ACTIVE is not current")
        if self._active is not None and self._active.binding == active.binding:
            self._require_current(server, _SERVER)
            if self._ingress is None or self.active_snapshot() is None:
                raise ControlBindingError("active ingress is unavailable")
            return self._ingress
        pending = self._matching_pending(server, active.binding, BindingPhase.COMMIT)
        controller = self._connection_from_lease(pending.controller_lease)
        if (
            pending.committed is None
            or pending.committed.last_epoch != pending.binding.session_epoch
            or controller is None
            or controller.connection_id != pending.controller_lease.connection_id
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
        if not self._guard_current(pending, BindingPhase.COMMIT):
            self._stage.unbind_minecraft_observations(observation.observation_scope)
            raise ControlBindingError("active binding changed during stage bind")
        ingress = TypedMinecraftObservationIngress(binding=observation, target=self._stage, now=self._utc_now)
        self._active = ActiveBinding(
            pending.binding,
            pending.server_lease,
            pending.controller_lease,
        )
        pending.phase = BindingPhase.ACTIVE
        self._pending.pop(pending.binding.session_id, None)
        self._v2_tickets.pop(pending.binding.session_id, None)
        self._ingress = ingress
        self._quarantined = False
        self._generation += 1
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
            or pending.server_lease.connection_id != server.connection_id
            or not self._guard_current(pending, phase)
        ):
            raise ControlBindingError("binding evidence does not match pending state")
        return pending

    def _require_current(self, connection: AuthenticatedRoleConnection, role: str) -> AuthenticatedRoleLease:
        lease = self._role_owner.current(role)
        expected_install = self._policy.server_install_id if role == _SERVER else self._policy.client_install_id
        if (
            connection.role != role
            or connection.install_id != expected_install
            or lease is None
            or lease.connection_id != connection.connection_id
            or lease.handshake is not connection.handshake
            or not self._role_owner.is_current(lease)
        ):
            raise ControlBindingError("authenticated origin is not current")
        return lease

    @staticmethod
    def _connection_from_lease(lease: AuthenticatedRoleLease | None) -> AuthenticatedRoleConnection | None:
        if lease is None:
            return None
        return AuthenticatedRoleConnection(connection_id=lease.connection_id, handshake=lease.handshake)

    def _leases_current(self, server: AuthenticatedRoleLease, controller: AuthenticatedRoleLease) -> bool:
        return self._role_owner.is_current(server) and self._role_owner.is_current(controller)

    def _withdrawal_target(self, withdrawal: RoleWithdrawal) -> AuthenticatedRoleLease | None:
        candidates = []
        if self._active is not None:
            candidates.extend((self._active.server_lease, self._active.controller_lease))
        for pending in self._pending.values():
            candidates.extend((pending.server_lease, pending.controller_lease))
        for lease in candidates:
            if (
                withdrawal.role == lease.role
                and withdrawal.connection_id == lease.connection_id
                and withdrawal.generation == lease.generation
            ):
                return lease
        return None

    def _fence_prepare_operation(
        self,
        generation: int,
        active: ActiveBinding | None,
        pending: tuple[_PendingBinding, ...],
        server: AuthenticatedRoleLease,
        controller: AuthenticatedRoleLease,
    ) -> None:
        if self._prepare_operation_current(generation, active, pending, server, controller):
            self._fence_local("QUARANTINE")

    def _prepare_operation_current(
        self,
        generation: int,
        active: ActiveBinding | None,
        pending: tuple[_PendingBinding, ...],
        server: AuthenticatedRoleLease,
        controller: AuthenticatedRoleLease,
    ) -> bool:
        actual_pending = tuple(self._pending.values())
        return (
            self._generation == generation
            and self._active is active
            and len(actual_pending) == len(pending)
            and all(actual is captured for actual, captured in zip(actual_pending, pending))
            and self._leases_current(server, controller)
        )

    def _guard_current(self, pending: _PendingBinding, phase: BindingPhase) -> bool:
        return (
            self._pending.get(pending.binding.session_id) is pending
            and pending.phase is phase
            and self._leases_current(pending.server_lease, pending.controller_lease)
        )

    async def _fence(self, reason: str) -> None:
        command = self._fence_local(reason)
        await self._best_effort_revoke(command)

    async def _cancelled_fence(self, reason: str, cancellation: asyncio.CancelledError) -> None:
        command = self._fence_local(reason)
        await self._best_effort_revoke(command)
        raise cancellation

    async def _fence_pending(self, pending: _PendingBinding, reason: str) -> None:
        if self._pending.get(pending.binding.session_id) is not pending:
            return
        await self._fence(reason)

    async def _cancelled_pending_fence(
        self,
        pending: _PendingBinding,
        reason: str,
        cancellation: asyncio.CancelledError,
    ) -> None:
        await self._fence_pending(pending, reason)
        raise cancellation

    def _fence_local(self, reason: str) -> BindingRevoke | None:
        """Synchronously remove all local authority before any cleanup await."""
        binding = self._active.binding if self._active is not None else None
        if binding is None and self._pending:
            binding = next(iter(self._pending.values())).binding
        handoffs = [(pending.preflight_handle, pending.preflight) for pending in self._pending.values() if pending.v2]
        for ticket in self._v2_tickets.values():
            ticket.state = V2DispatchState.STALE_OR_UNKNOWN
            ticket.ready = None
        observation = None
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
        self._active = None
        self._ingress = None
        self._pending.clear()
        self._v2_tickets.clear()
        self._quarantined = True
        self._generation += 1
        cleanup_error = None
        for handle, snapshot in handoffs:
            try:
                self._obsolete_preflight(handle, snapshot)
            except BaseException as error:
                cleanup_error = cleanup_error or error
        if observation is not None:
            try:
                self._stage.unbind_minecraft_observations(observation.observation_scope)
            except BaseException as error:
                cleanup_error = cleanup_error or error
        if cleanup_error is not None:
            raise cleanup_error
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
