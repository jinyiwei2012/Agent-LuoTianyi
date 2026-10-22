"""Offline pre-bound Minecraft action port over canonical envelopes.

The port owns correlation and admission waiting, not transport, authentication,
server policy, permits, receipts, or game success.  An external owner supplies
the current immutable active binding and routes authenticated server envelopes
back through :meth:`receive_authoritative`.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol
from uuid import uuid4

from src.adapter.minecraft.action_ledger import (
    ActionLedgerError,
    ActionRecord,
    ActionRecordPhase,
    MinecraftActionLedger,
    RevokeRequirement,
    TrustedServerOrigin,
)
from src.adapter.minecraft.control_binding import BindingTuple
from src.adapter.minecraft.wire import MinecraftEnvelope, decode_message, encode_message
from src.domain.agent.minecraft import (
    MinecraftActionRequest,
    MinecraftBoundScope,
    MinecraftCapabilities,
    MinecraftCapabilityAvailability,
    MinecraftDispatchResult,
    MinecraftIntent,
    MinecraftPortAdmission,
)

_BASIS_TTL = timedelta(seconds=1)
_CLEANUP_TIMEOUT_SECONDS = 0.1
_CLOCK_CHECK_INTERVAL_SECONDS = 0.05


class CanonicalActionSender(Protocol):
    async def send(self, envelope: MinecraftEnvelope) -> None: ...

    async def revoke(self, requirement: RevokeRequirement) -> None: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class TrustedActionObservation:
    """Current authenticated observation selected outside model output."""

    observation_id: str
    server_boot_id: str
    server_revision: int
    observed_at: datetime
    capabilities: MinecraftCapabilities

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value.strip() for value in (self.observation_id, self.server_boot_id)):
            raise ValueError("invalid trusted observation identity")
        if type(self.server_revision) is not int or self.server_revision < 0:
            raise ValueError("invalid trusted observation revision")
        if (
            not isinstance(self.observed_at, datetime)
            or self.observed_at.tzinfo is None
            or self.observed_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("invalid trusted observation time")
        if not isinstance(self.capabilities, MinecraftCapabilities):
            raise TypeError("invalid trusted observation capabilities")


@dataclass(frozen=True, slots=True, kw_only=True)
class ActiveActionContext:
    """Exact live coordinator state required for one port operation."""

    binding: BindingTuple
    server_origin: TrustedServerOrigin
    observation: TrustedActionObservation | None

    def __post_init__(self) -> None:
        if not isinstance(self.binding, BindingTuple) or not isinstance(self.server_origin, TrustedServerOrigin):
            raise TypeError("invalid active action context")


@dataclass(slots=True)
class _PendingResult:
    local_request_id: str
    context: ActiveActionContext
    future: asyncio.Future[MinecraftPortAdmission]


class OfflineMinecraftActionPort:
    """Domain ``MinecraftActionPort`` backed by a reviewed action ledger."""

    def __init__(
        self,
        *,
        ledger: MinecraftActionLedger,
        sender: CanonicalActionSender,
        active_context: Callable[[], ActiveActionContext | None],
        monotonic_now: Callable[[], float],
        utc_now: Callable[[], datetime],
        admission_timeout_seconds: float = 5.0,
        message_id: Callable[[], str] | None = None,
    ) -> None:
        if (
            not isinstance(ledger, MinecraftActionLedger)
            or not callable(active_context)
            or not callable(monotonic_now)
            or not callable(utc_now)
        ):
            raise TypeError("invalid action port dependencies")
        if (
            type(admission_timeout_seconds) not in (int, float)
            or isinstance(admission_timeout_seconds, bool)
            or not 0 < admission_timeout_seconds <= 5
        ):
            raise ValueError("admission timeout must be within five seconds")
        self._ledger = ledger
        self._sender = sender
        self._active_context = active_context
        self._monotonic_now = monotonic_now
        self._utc_now = utc_now
        self._timeout = float(admission_timeout_seconds)
        self._message_id = message_id or (lambda: str(uuid4()))
        self._pending: dict[str, _PendingResult] = {}
        self._local_to_wire: dict[str, tuple[str, BindingTuple]] = {}
        self._request_records: dict[str, ActionRecord] = {}
        self._send_tasks: dict[str, asyncio.Task[None]] = {}
        self._last_monotonic: float | None = None
        self._clock_failed = False
        self._closed = False

    @property
    def scope(self) -> MinecraftBoundScope:
        context = self._require_context()
        return MinecraftBoundScope(
            world_id=context.binding.world_id,
            body_id=context.binding.body_id,
            session_epoch=context.binding.session_epoch,
        )

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def correlation_count(self) -> int:
        self._cleanup_correlations()
        return len(self._local_to_wire)

    @property
    def owned_send_task_count(self) -> int:
        return len(self._send_tasks)

    async def submit(self, request: MinecraftActionRequest) -> MinecraftDispatchResult:
        if not isinstance(request, MinecraftActionRequest) or request.intent is MinecraftIntent.STOP:
            raise TypeError("submit requires a non-STOP MinecraftActionRequest")
        self._cleanup_correlations()
        context = self._require_context(request.scope)
        if request.request_id in self._local_to_wire:
            raise ValueError("Minecraft local request identity is already active")
        envelope = self._build_request(request, context)
        record = self._ledger.reserve_request(envelope, local_action_id=request.request_id)
        self._local_to_wire[request.request_id] = (envelope.message_id, context.binding)
        self._request_records[envelope.message_id] = record
        return await self._send_and_wait(request.request_id, envelope, context)

    async def cancel(self, request: MinecraftActionRequest) -> MinecraftDispatchResult:
        if not isinstance(request, MinecraftActionRequest) or request.intent is not MinecraftIntent.STOP:
            raise TypeError("cancel requires a STOP MinecraftActionRequest")
        self._cleanup_correlations()
        context = self._require_context(request.scope)
        correlation = self._local_to_wire.get(request.target_request_id)
        if correlation is None:
            return MinecraftDispatchResult(request_id=request.request_id, admission=MinecraftPortAdmission.DECLINED)
        target_wire_id, target_binding = correlation
        if target_binding != context.binding:
            return MinecraftDispatchResult(request_id=request.request_id, admission=MinecraftPortAdmission.DECLINED)
        try:
            target = self._ledger.record(target_wire_id)
        except ActionLedgerError as error:
            if str(error) == "unknown request":
                self._local_to_wire.pop(request.target_request_id, None)
                return MinecraftDispatchResult(request_id=request.request_id, admission=MinecraftPortAdmission.DECLINED)
            raise
        if target.binding != context.binding:
            return MinecraftDispatchResult(request_id=request.request_id, admission=MinecraftPortAdmission.DECLINED)
        envelope = self._build_cancel(request, context, target_wire_id)
        self._ledger.reserve_cancel(envelope)
        return await self._send_and_wait(request.request_id, envelope, context)

    def receive_authoritative(self, envelope: MinecraftEnvelope, *, origin: TrustedServerOrigin) -> None:
        context = self._active_context()
        if self._closed or context is None or context.server_origin != origin:
            raise ActionLedgerError("action port context is not current")
        self._ledger.ingest(envelope, origin=origin)
        if envelope.type == "admission":
            request_id = envelope.payload["requestId"]
            pending = self._pending.get(request_id)
            if pending is not None and pending.context == context:
                self._resolve(pending, self._ledger.dispatch_admission(request_id))
        elif envelope.type == "cancel_result":
            cancel_id = envelope.payload["cancelId"]
            pending = self._pending.get(cancel_id)
            if pending is not None and pending.context == context:
                result = envelope.payload["result"]
                admission = {
                    "REQUESTED": MinecraftPortAdmission.ACCEPTED,
                    "ALREADY_TERMINAL": MinecraftPortAdmission.ACCEPTED,
                    "NOT_FOUND": MinecraftPortAdmission.DECLINED,
                    "REJECTED": MinecraftPortAdmission.DECLINED,
                    "UNKNOWN": MinecraftPortAdmission.UNKNOWN,
                }[result]
                self._resolve(pending, admission)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        requirements: list[RevokeRequirement] = []
        for request_id, pending in tuple(self._pending.items()):
            requirements.append(self._mark_unknown(request_id, pending.context.binding))
            self._resolve(pending, MinecraftPortAdmission.UNKNOWN)
        self._local_to_wire.clear()
        self._request_records.clear()
        tasks = tuple(self._send_tasks.values())
        for task in tasks:
            task.cancel()
        for requirement in requirements:
            await self._best_effort_revoke(requirement)
        if tasks:
            done, _pending = await asyncio.wait(tasks, timeout=_CLEANUP_TIMEOUT_SECONDS)
            for task in done:
                try:
                    task.result()
                except BaseException:
                    pass

    async def _send_and_wait(
        self,
        local_request_id: str,
        envelope: MinecraftEnvelope,
        context: ActiveActionContext,
    ) -> MinecraftDispatchResult:
        deadline = self._monotonic() + self._timeout
        loop = asyncio.get_running_loop()
        loop_deadline = loop.time() + self._timeout
        pending = _PendingResult(local_request_id, context, loop.create_future())
        self._pending[envelope.message_id] = pending
        send_task = asyncio.create_task(self._sender.send(envelope), name="minecraft-action-send")
        self._send_tasks[envelope.message_id] = send_task
        send_task.add_done_callback(lambda done: self._send_done(envelope.message_id, done))
        try:
            admission = await self._await_send_and_admission(
                envelope.message_id,
                pending,
                send_task,
                deadline,
                loop_deadline,
                context.binding,
            )
            if self._active_context() != context or self._closed:
                admission = await self._unknown_with_revoke(envelope.message_id, context.binding)
            return MinecraftDispatchResult(request_id=local_request_id, admission=admission)
        except asyncio.CancelledError as cancellation:
            requirement = self._mark_unknown(envelope.message_id, context.binding)
            send_task.cancel()
            await self._best_effort_revoke(requirement)
            raise cancellation
        except Exception:
            requirement = self._mark_unknown(envelope.message_id, context.binding)
            send_task.cancel()
            await self._best_effort_revoke(requirement)
            return MinecraftDispatchResult(request_id=local_request_id, admission=MinecraftPortAdmission.UNKNOWN)
        finally:
            self._pending.pop(envelope.message_id, None)
            if not pending.future.done():
                pending.future.cancel()

    async def _await_send_and_admission(
        self,
        wire_request_id: str,
        pending: _PendingResult,
        send_task: asyncio.Task[None],
        deadline: float,
        loop_deadline: float,
        original_binding: BindingTuple,
    ) -> MinecraftPortAdmission:
        send_finished = False
        while True:
            remaining = min(deadline - self._monotonic(), loop_deadline - asyncio.get_running_loop().time())
            if remaining <= 0:
                send_task.cancel()
                return await self._unknown_with_revoke(wire_request_id, original_binding)
            watched = {pending.future}
            if not send_finished:
                watched.add(send_task)
            done, _ = await asyncio.wait(
                watched,
                timeout=min(remaining, _CLOCK_CHECK_INTERVAL_SECONDS),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                continue
            if pending.future in done:
                await self._cancel_send_if_running(send_task)
                return pending.future.result()
            if send_task in done:
                send_finished = True
                if not self._send_succeeded(send_task, pending):
                    return await self._unknown_with_revoke(wire_request_id, original_binding)

    async def _unknown_with_revoke(
        self, wire_request_id: str, original_binding: BindingTuple
    ) -> MinecraftPortAdmission:
        requirement = self._mark_unknown(wire_request_id, original_binding)
        await self._best_effort_revoke(requirement)
        return MinecraftPortAdmission.UNKNOWN

    def _build_request(self, request: MinecraftActionRequest, context: ActiveActionContext) -> MinecraftEnvelope:
        now = self._trusted_utc_now()
        basis = self._basis(request, context, now)
        intent = self._intent(request)
        return self._canonical_envelope(
            context,
            kind="request",
            timestamp=now,
            payload={
                "deadline": self._utc_text(now + timedelta(seconds=self._timeout)),
                "basis": basis,
                "intent": intent,
            },
        )

    def _build_cancel(
        self,
        request: MinecraftActionRequest,
        context: ActiveActionContext,
        target_wire_id: str,
    ) -> MinecraftEnvelope:
        now = self._trusted_utc_now()
        return self._canonical_envelope(
            context,
            kind="cancel",
            timestamp=now,
            payload={
                "mode": "TARGET",
                "targetRequestId": target_wire_id,
                "targetEpoch": context.binding.session_epoch,
                "deadline": self._utc_text(now + timedelta(seconds=self._timeout)),
            },
        )

    def _canonical_envelope(
        self,
        context: ActiveActionContext,
        *,
        kind: str,
        timestamp: datetime,
        payload: dict,
    ) -> MinecraftEnvelope:
        binding = context.binding
        envelope = MinecraftEnvelope(
            protocol="luosandbox.minecraft.v1",
            type=kind,
            actor_id=binding.actor_id,
            world_id=binding.world_id,
            body_id=binding.body_id,
            session_id=binding.session_id,
            session_epoch=binding.session_epoch,
            message_id=self._next_message_id(),
            timestamp=timestamp,
            payload=payload,
        )
        return decode_message(encode_message(envelope))

    def _basis(self, request: MinecraftActionRequest, context: ActiveActionContext, now: datetime):
        observation = context.observation
        if request.intent is MinecraftIntent.QUERY and request.basis_observation_id is None:
            return None
        if (
            observation is None
            or request.basis_observation_id != observation.observation_id
            or observation.server_boot_id != context.binding.server_boot_id
        ):
            raise ValueError("Minecraft action basis is not current")
        if observation.capabilities.availability(request.intent) is not MinecraftCapabilityAvailability.AVAILABLE:
            raise ValueError("Minecraft capability is unavailable")
        if request.intent is not MinecraftIntent.QUERY and now - observation.observed_at > _BASIS_TTL:
            raise ValueError("Minecraft action basis is stale")
        return {
            "observationId": observation.observation_id,
            "serverBootId": observation.server_boot_id,
            "serverRevision": observation.server_revision,
        }

    @staticmethod
    def _intent(request: MinecraftActionRequest) -> dict:
        if request.intent is MinecraftIntent.QUERY:
            return {"kind": "QUERY", "queryKind": "READ_ONLY_SUMMARY"}
        if request.intent is MinecraftIntent.PUBLIC_SAY:
            return {"kind": "PUBLIC_SAY", "message": request.public_message}
        target = request.target
        result = {
            "kind": request.intent.value,
            "dimensionId": target.dimension_id,
            "target": {"x": target.position.x, "y": target.position.y, "z": target.position.z},
        }
        if request.intent is MinecraftIntent.BOUNDED_WALK:
            result["maximumDistance"] = request.maximum_distance
        return result

    def _require_context(self, scope: MinecraftBoundScope | None = None) -> ActiveActionContext:
        if self._closed or self._clock_failed:
            raise RuntimeError("Minecraft action port is closed")
        context = self._active_context()
        if context is None or not isinstance(context, ActiveActionContext):
            raise RuntimeError("Minecraft action context is unavailable")
        expected = MinecraftBoundScope(
            world_id=context.binding.world_id,
            body_id=context.binding.body_id,
            session_epoch=context.binding.session_epoch,
        )
        if scope is not None and scope != expected:
            raise ValueError("Minecraft action scope does not match active binding")
        return context

    def _trusted_utc_now(self) -> datetime:
        value = self._utc_now()
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() != timedelta(0)
            or value.microsecond % 1000 != 0
        ):
            raise RuntimeError("invalid trusted UTC clock")
        return value.astimezone(timezone.utc)

    def _next_message_id(self) -> str:
        value = self._message_id()
        if not isinstance(value, str):
            raise RuntimeError("invalid message id generator")
        return value

    @staticmethod
    def _utc_text(value: datetime) -> str:
        return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"

    def _mark_unknown(self, wire_request_id: str, original_binding: BindingTuple) -> RevokeRequirement:
        try:
            return self._ledger.mark_send_unknown(wire_request_id)
        except ActionLedgerError:
            record = self._request_records.get(wire_request_id)
            if record is not None and record.phase is not ActionRecordPhase.TERMINAL:
                # The ledger's clock check may itself reject after the same
                # rollback. Preserve the already-reserved correlation as
                # uncertain without healing or lowering either trusted clock.
                record.phase = ActionRecordPhase.UNCERTAIN
                record.quarantined = True
                self._ledger._effect_fence_quarantined = True
            return RevokeRequirement(binding=original_binding, reason="OUTCOME_UNPROVEN")

    def _cleanup_correlations(self) -> None:
        for local_request_id, (wire_request_id, _binding) in tuple(self._local_to_wire.items()):
            try:
                self._ledger.record(wire_request_id)
            except ActionLedgerError as error:
                if str(error) == "unknown request":
                    self._local_to_wire.pop(local_request_id, None)
                    self._request_records.pop(wire_request_id, None)

    def _monotonic(self) -> float:
        if self._clock_failed:
            raise RuntimeError("monotonic clock previously failed")
        value = self._monotonic_now()
        if type(value) not in (int, float) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
            self._clock_failed = True
            raise RuntimeError("invalid monotonic clock")
        value = float(value)
        if self._last_monotonic is not None and value < self._last_monotonic:
            self._clock_failed = True
            raise RuntimeError("monotonic clock rolled back")
        self._last_monotonic = value
        return value

    def _send_done(self, wire_request_id: str, task: asyncio.Task[None]) -> None:
        if self._send_tasks.get(wire_request_id) is task:
            self._send_tasks.pop(wire_request_id, None)

    @staticmethod
    async def _drain_send_task(task: asyncio.Task[None]) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=_CLEANUP_TIMEOUT_SECONDS)
        except BaseException:
            return

    async def _cancel_send_if_running(self, task: asyncio.Task[None]) -> None:
        if task.done():
            return
        task.cancel()
        await self._drain_send_task(task)

    def _send_succeeded(self, task: asyncio.Task[None], pending: _PendingResult) -> bool:
        try:
            task.result()
            return True
        except asyncio.CancelledError:
            if self._closed and pending.future.done():
                return False
            raise
        except Exception:
            return False

    async def _best_effort_revoke(self, requirement: RevokeRequirement) -> None:
        try:
            await asyncio.wait_for(self._sender.revoke(requirement), timeout=_CLEANUP_TIMEOUT_SECONDS)
        except BaseException:
            return

    @staticmethod
    def _resolve(pending: _PendingResult, admission: MinecraftPortAdmission) -> None:
        if not pending.future.done():
            pending.future.set_result(admission)
