"""Emit a typed terminal fact only from an immutable receipt accepted by the ledger."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

import src.domain.agent as d
from src.adapter.minecraft.action_ledger import ActionRecord, MinecraftActionLedger
from src.adapter.minecraft.control_binding import BindingTuple
from src.adapter.minecraft.wire import MinecraftEnvelope


class TerminalOutcomeSink(Protocol):
    async def submit(self, outcome: d.MinecraftActionOutcomeObserved) -> bool: ...


class TerminalProjectionAdmission(str, Enum):
    ACCEPTED = "accepted"
    DECLINED = "declined"
    UNKNOWN = "unknown"


class _ProjectionState(str, Enum):
    SUBMITTING = "submitting"
    ACCEPTED = "accepted"
    UNCERTAIN = "uncertain"


@dataclass(slots=True)
class _Projection:
    receipt_id: str
    state: _ProjectionState
    task: asyncio.Task[bool] | None = None


class MinecraftTerminalIngress:
    """Bounded at-most-once process-local terminal-outcome projection."""

    def __init__(
        self,
        *,
        ledger: MinecraftActionLedger,
        binding: BindingTuple,
        character_id: str,
        sink: TerminalOutcomeSink | None,
    ) -> None:
        if not isinstance(ledger, MinecraftActionLedger) or not isinstance(binding, BindingTuple):
            raise TypeError("invalid terminal ingress dependencies")
        if not isinstance(character_id, str) or not character_id.strip():
            raise ValueError("character_id must be nonblank")
        self._ledger = ledger
        self._binding = binding
        self._character_id = character_id
        self._sink = sink
        self._projections: dict[str, _Projection] = {}
        self._closed = False

    @property
    def emitted_count(self) -> int:
        self._prune()
        return sum(item.state is _ProjectionState.ACCEPTED for item in self._projections.values())

    @property
    def owned_count(self) -> int:
        self._prune()
        return len(self._projections)

    async def emit(self, receipt: MinecraftEnvelope) -> TerminalProjectionAdmission:
        fact = self.fact(receipt)
        if fact is None or self._closed:
            return TerminalProjectionAdmission.DECLINED
        record = self._ledger.record(receipt.payload["requestId"])
        prior = self._projections.get(record.request_id)
        if prior is not None:
            if prior.receipt_id != receipt.message_id:
                return TerminalProjectionAdmission.UNKNOWN
            if prior.state is _ProjectionState.ACCEPTED:
                return TerminalProjectionAdmission.ACCEPTED
            if prior.state is _ProjectionState.UNCERTAIN:
                return TerminalProjectionAdmission.UNKNOWN
            return await self._wait_owned(record.request_id, prior)
        if self._sink is None:
            return TerminalProjectionAdmission.DECLINED
        if len(self._projections) >= 32:
            return TerminalProjectionAdmission.UNKNOWN
        projection = _Projection(receipt.message_id, _ProjectionState.SUBMITTING)
        self._projections[record.request_id] = projection
        projection.task = asyncio.create_task(self._sink.submit(fact), name="minecraft-terminal-submit")
        projection.task.add_done_callback(
            lambda done, request_id=record.request_id, owner=projection: self._submission_done(request_id, owner, done)
        )
        return await self._wait_owned(record.request_id, projection)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = tuple(
            item.task
            for item in self._projections.values()
            if item.state is _ProjectionState.SUBMITTING and item.task is not None
        )
        for item in self._projections.values():
            if item.state is _ProjectionState.SUBMITTING:
                item.state = _ProjectionState.UNCERTAIN
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=0.1)

    def fact(self, receipt: MinecraftEnvelope) -> d.MinecraftActionOutcomeObserved | None:
        if not isinstance(receipt, MinecraftEnvelope) or receipt.type != "receipt":
            return None
        record = self._ledger.record(receipt.payload["requestId"])
        if record.admission is None or record.admission.payload["decision"] != "ACCEPTED" or record.receipt != receipt:
            return None
        return self._fact(record, receipt)

    def _fact(self, record: ActionRecord, receipt: MinecraftEnvelope) -> d.MinecraftActionOutcomeObserved:
        payload, stamp, outcome = receipt.payload, receipt.payload["stamp"], receipt.payload["outcome"]
        state = outcome["state"]
        body_state = None
        if state is not None:
            body_state = d.MinecraftBodyState(
                dimension_id=state["dimensionId"],
                position=d.MinecraftPosition(**state["position"]),
                yaw=state["yaw"],
                pitch=state["pitch"],
            )
        return d.MinecraftActionOutcomeObserved(
            stimulus_id=f"minecraft-outcome:{receipt.message_id}",
            schema_version=1,
            occurred_at=receipt.timestamp,
            source=d.StimulusSource.WORLD,
            target_character_ids=(self._character_id,),
            user_id=None,
            ephemeral=True,
            receipt_id=receipt.message_id,
            request_id=record.request_id,
            local_action_id=record.local_action_id,
            body_id=self._binding.body_id,
            session_id=self._binding.session_id,
            session_epoch=self._binding.session_epoch,
            server_boot_id=stamp["serverBootId"],
            server_revision=stamp["serverRevision"],
            server_tick=stamp["serverTick"],
            evidence_refs=tuple(d.EvidenceRef(evidence_id=value) for value in stamp["evidence"]),
            outcome=d.MinecraftTerminalOutcome(
                status=d.MinecraftTerminalStatus(payload["status"]),
                reason=payload["reason"],
                kind=d.MinecraftOutcomeKind(outcome["kind"]),
                effect=d.MinecraftEffect(outcome["effect"]),
                state=body_state,
                observation_id=outcome["observationId"],
            ),
        )

    def _prune(self) -> None:
        for request_id, projection in tuple(self._projections.items()):
            if projection.state is _ProjectionState.SUBMITTING:
                continue
            try:
                self._ledger.record(request_id)
            except Exception:
                self._projections.pop(request_id, None)

    async def _wait_owned(self, request_id: str, projection: _Projection) -> TerminalProjectionAdmission:
        task = projection.task
        if task is None:
            return TerminalProjectionAdmission.UNKNOWN
        try:
            accepted = await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            projection.state = _ProjectionState.UNCERTAIN
            return TerminalProjectionAdmission.UNKNOWN
        if type(accepted) is not bool:
            projection.state = _ProjectionState.UNCERTAIN
            return TerminalProjectionAdmission.UNKNOWN
        if accepted:
            projection.state = _ProjectionState.ACCEPTED
            return TerminalProjectionAdmission.ACCEPTED
        if self._projections.get(request_id) is projection:
            self._projections.pop(request_id, None)
        return TerminalProjectionAdmission.DECLINED

    def _submission_done(self, request_id: str, projection: _Projection, task: asyncio.Task[bool]) -> None:
        if self._projections.get(request_id) is not projection:
            return
        if task.cancelled():
            projection.state = _ProjectionState.UNCERTAIN
            return
        try:
            accepted = task.result()
        except Exception:
            projection.state = _ProjectionState.UNCERTAIN
            return
        if accepted is True:
            projection.state = _ProjectionState.ACCEPTED
        elif accepted is False:
            self._projections.pop(request_id, None)
        else:
            projection.state = _ProjectionState.UNCERTAIN
