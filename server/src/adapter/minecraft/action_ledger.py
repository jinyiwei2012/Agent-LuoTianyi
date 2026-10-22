"""Bounded offline correlation ledger for authoritative Minecraft control results.

The ledger owns no transport and grants no game permission.  Callers provide
strictly decoded canonical envelopes and immutable authenticated server origin.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable

from src.adapter.minecraft.control_binding import AuthenticatedRoleConnection, BindingTuple
from src.adapter.minecraft.wire import MinecraftEnvelope
from src.domain.agent.minecraft import MinecraftPortAdmission

_SERVER_ROLE = "SERVER_AUTHORITY"
_MAX_ACTION_RECORDS = 32
_MAX_PENDING_JOINS = 8
_MAX_CONTROL_RECORDS = 64
_RETENTION_SECONDS = 300.0
_REQUEST_PAST_LIMIT = timedelta(seconds=5)
_REQUEST_FUTURE_LIMIT = timedelta(seconds=1)


class ActionLedgerError(RuntimeError):
    """Fail-closed rejection without changing an already known canonical result."""


class ActionRecordPhase(str, Enum):
    AWAITING_ADMISSION = "AWAITING_ADMISSION"
    ADMITTED = "ADMITTED"
    TERMINAL = "TERMINAL"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True, slots=True, kw_only=True)
class TrustedServerOrigin:
    """Current immutable authenticated SERVER connection used as message origin."""

    connection: AuthenticatedRoleConnection
    expected_install_id: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.connection, AuthenticatedRoleConnection)
            or self.connection.role != _SERVER_ROLE
            or self.connection.install_id != self.expected_install_id
        ):
            raise ValueError("invalid trusted server origin")


@dataclass(slots=True)
class ActionRecord:
    local_action_id: str
    request: MinecraftEnvelope
    binding: BindingTuple
    created_at: float
    phase: ActionRecordPhase = ActionRecordPhase.AWAITING_ADMISSION
    admission: MinecraftEnvelope | None = None
    receipt: MinecraftEnvelope | None = None
    buffered_receipt: MinecraftEnvelope | None = None
    terminal_at: float | None = None
    quarantined: bool = False

    @property
    def request_id(self) -> str:
        return self.request.message_id


@dataclass(slots=True)
class ReconcileRecord:
    request: MinecraftEnvelope
    created_at: float
    result: MinecraftEnvelope | None = None
    target_request_id: str | None = None
    admission_id: str | None = None
    receipt_id: str | None = None
    terminal_at: float | None = None
    uncertain: bool = False


@dataclass(slots=True)
class _ControlRecord:
    request: MinecraftEnvelope
    created_at: float
    result: MinecraftEnvelope | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ReconcileView:
    reconcile_id: str
    result: str | None
    complete: bool
    target_request_id: str | None
    admission_id: str | None
    receipt_id: str | None
    fence_cleared: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class RevokeRequirement:
    binding: BindingTuple
    reason: str


class MinecraftActionLedger:
    """Single-event-loop action/admission/receipt/reconcile correlation owner."""

    def __init__(
        self,
        *,
        binding: BindingTuple,
        server_origin: TrustedServerOrigin,
        monotonic_now: Callable[[], float],
        utc_now: Callable[[], datetime],
    ) -> None:
        if not isinstance(binding, BindingTuple) or not isinstance(server_origin, TrustedServerOrigin):
            raise TypeError("invalid ledger binding")
        if not callable(monotonic_now) or not callable(utc_now):
            raise TypeError("ledger clocks must be callable")
        self._binding = binding
        self._origin = server_origin
        self._monotonic_now = monotonic_now
        self._utc_now = utc_now
        self._records: dict[str, ActionRecord] = {}
        self._reconciles: dict[str, ReconcileRecord] = {}
        self._cancels: dict[str, _ControlRecord] = {}
        self._messages: dict[str, MinecraftEnvelope] = {}
        self._stamp_events: dict[int, str] = {}
        self._event_revisions: dict[str, int] = {}
        self._last_monotonic: float | None = None
        self._effect_fence_quarantined = False

    @property
    def effect_fence_quarantined(self) -> bool:
        return self._effect_fence_quarantined

    @property
    def action_count(self) -> int:
        return len(self._records)

    @property
    def metadata_counts(self) -> dict[str, int]:
        """Expose bounded table sizes for deterministic offline verification."""
        return {
            "actions": len(self._records),
            "reconciles": len(self._reconciles),
            "cancels": len(self._cancels),
            "messages": len(self._messages),
            "stamp_events": len(self._stamp_events),
            "event_revisions": len(self._event_revisions),
        }

    def reserve_request(self, request: MinecraftEnvelope, *, local_action_id: str) -> ActionRecord:
        now = self._maintain()
        self._validate_outbound(request, "request")
        _nonblank(local_action_id, "local action id")
        existing = self._records.get(request.message_id)
        if existing is not None:
            if existing.request == request and existing.local_action_id == local_action_id:
                return existing
            self._violation(existing, "request identity conflict")
        if len(self._records) >= _MAX_ACTION_RECORDS:
            raise ActionLedgerError("action record capacity")
        self._validate_request_time(request.timestamp)
        self._validate_new_message(request, None)
        record = ActionRecord(local_action_id=local_action_id, request=request, binding=self._binding, created_at=now)
        self._records[request.message_id] = record
        self._commit_message(request)
        return record

    def mark_send_unknown(self, request_id: str) -> RevokeRequirement:
        self._maintain()
        record = self._require_record(request_id)
        if record.phase is not ActionRecordPhase.TERMINAL:
            record.phase = ActionRecordPhase.UNCERTAIN
            record.quarantined = True
            self._effect_fence_quarantined = True
        return RevokeRequirement(binding=record.binding, reason="OUTCOME_UNPROVEN")

    def reserve_cancel(self, cancel: MinecraftEnvelope) -> MinecraftEnvelope:
        now = self._maintain()
        self._validate_outbound(cancel, "cancel")
        return self._reserve_control(cancel, now)

    def reserve_reconcile(self, reconcile: MinecraftEnvelope) -> ReconcileRecord:
        now = self._maintain()
        self._validate_outbound(reconcile, "reconcile")
        existing = self._reconciles.get(reconcile.message_id)
        if existing is not None:
            if existing.request == reconcile:
                return existing
            raise ActionLedgerError("reconcile identity conflict")
        if len(self._reconciles) >= _MAX_PENDING_JOINS:
            raise ActionLedgerError("reconcile capacity")
        self._validate_new_message(reconcile, None)
        record = ReconcileRecord(request=reconcile, created_at=now)
        self._reconciles[reconcile.message_id] = record
        self._commit_message(reconcile)
        return record

    def ingest(self, envelope: MinecraftEnvelope, *, origin: TrustedServerOrigin) -> None:
        now = self._maintain()
        self._validate_origin(origin)
        if envelope.type not in {"admission", "receipt", "cancel_result", "reconcile_result"}:
            raise ActionLedgerError("unsupported authoritative message")
        target = self._target_record(envelope)
        if self._existing_message_is_duplicate(envelope, target):
            return
        stamp = self._validate_inbound_binding(envelope, target)
        apply = self._validate_ingest(envelope, target, now)
        self._commit_message(envelope, stamp)
        apply()
        self._refresh_reconcile_terminals(now)

    def _validate_ingest(self, envelope: MinecraftEnvelope, target: ActionRecord | None, now: float):
        if envelope.type == "admission":
            return self._validate_admission(target, envelope, now)
        if envelope.type == "receipt":
            return self._validate_receipt(target, envelope, now)
        if envelope.type == "cancel_result":
            return self._validate_cancel_result(envelope)
        return self._validate_reconcile_result(envelope)

    def dispatch_admission(self, request_id: str) -> MinecraftPortAdmission:
        self._maintain()
        record = self._require_record(request_id)
        if record.admission is None or record.quarantined:
            return MinecraftPortAdmission.UNKNOWN
        return (
            MinecraftPortAdmission.ACCEPTED
            if record.admission.payload["decision"] == "ACCEPTED"
            else MinecraftPortAdmission.DECLINED
        )

    def record(self, request_id: str) -> ActionRecord:
        self._maintain()
        return self._require_record(request_id)

    def reconcile_view(self, reconcile_id: str) -> ReconcileView:
        self._maintain()
        record = self._reconciles.get(reconcile_id)
        if record is None:
            raise ActionLedgerError("unknown reconcile")
        result = None if record.result is None else record.result.payload["result"]
        complete = self._reconcile_complete(record)
        return ReconcileView(
            reconcile_id=reconcile_id,
            result=result,
            complete=complete,
            target_request_id=record.target_request_id,
            admission_id=record.admission_id,
            receipt_id=record.receipt_id,
            fence_cleared=False,
        )

    def _validate_admission(self, record: ActionRecord, envelope: MinecraftEnvelope, now: float):
        if record.admission is not None:
            self._violation(record, "conflicting admission")
        decision = envelope.payload["decision"]
        if decision == "REJECTED" and record.buffered_receipt is not None:
            self._violation(record, "rejected request has receipt")
        if record.buffered_receipt is not None:
            self._validate_receipt_outcome(record, record.buffered_receipt)

        def apply() -> None:
            record.admission = envelope
            if decision == "REJECTED":
                self._terminal(record, now)
                return
            record.phase = ActionRecordPhase.ADMITTED
            if record.buffered_receipt is not None:
                buffered, record.buffered_receipt = record.buffered_receipt, None
                self._apply_receipt(record, buffered, now)

        return apply

    def _validate_receipt(self, record: ActionRecord, envelope: MinecraftEnvelope, now: float):
        if record.receipt is not None:
            self._violation(record, "conflicting receipt")
        if record.admission is None:
            if self._buffered_receipt_count() >= _MAX_PENDING_JOINS:
                record.phase = ActionRecordPhase.UNCERTAIN
                record.quarantined = True
                self._effect_fence_quarantined = True
                raise ActionLedgerError("receipt join capacity")
            self._validate_receipt_outcome(record, envelope)
            return lambda: setattr(record, "buffered_receipt", envelope)
        if record.admission.payload["decision"] != "ACCEPTED":
            self._violation(record, "receipt for rejected request")
        self._validate_receipt_outcome(record, envelope)
        return lambda: self._apply_receipt(record, envelope, now)

    def _validate_receipt_outcome(self, record: ActionRecord, envelope: MinecraftEnvelope) -> None:
        if envelope.payload["status"] == "SUCCEEDED":
            expected = {
                "QUERY": "SUMMARY",
                "PUBLIC_SAY": "PUBLIC_BROADCAST",
                "LOOK": "ORIENTATION",
                "BOUNDED_WALK": "POSITION",
            }[record.request.payload["intent"]["kind"]]
            if envelope.payload["outcome"]["kind"] != expected:
                self._violation(record, "receipt outcome does not match request intent")

    def _apply_receipt(self, record: ActionRecord, envelope: MinecraftEnvelope, now: float) -> None:
        record.receipt = envelope
        self._terminal(record, now)
        if envelope.payload["status"] == "UNKNOWN":
            record.quarantined = True
            self._effect_fence_quarantined = True

    def _validate_cancel_result(self, envelope: MinecraftEnvelope):
        cancel_id = envelope.payload["cancelId"]
        control = self._cancels.get(cancel_id)
        if control is None:
            raise ActionLedgerError("unsolicited cancel result")
        cancel = control.request
        if (
            envelope.payload["mode"] != cancel.payload["mode"]
            or envelope.payload["targetRequestId"] != cancel.payload["targetRequestId"]
            or envelope.payload["targetEpoch"] != cancel.payload["targetEpoch"]
        ):
            raise ActionLedgerError("cancel result does not match original cancel")
        existing = control.result
        if existing is not None and existing != envelope:
            raise ActionLedgerError("conflicting cancel result")
        return lambda: setattr(control, "result", envelope)

    def _validate_reconcile_result(self, envelope: MinecraftEnvelope):
        reconcile_id = envelope.payload["reconcileId"]
        record = self._reconciles.get(reconcile_id)
        if record is None:
            raise ActionLedgerError("unsolicited reconcile result")
        if record.result is not None and record.result != envelope:
            raise ActionLedgerError("conflicting reconcile result")
        payload = envelope.payload
        target_id = payload["targetRequestId"]
        if (
            target_id != record.request.payload["targetRequestId"]
            or payload["targetEpoch"] != record.request.payload["targetEpoch"]
        ):
            raise ActionLedgerError("reconcile result does not match original reconcile")
        if target_id is not None:
            target = self._records.get(target_id)
            if target is None or target.binding.session_epoch != payload["targetEpoch"]:
                raise ActionLedgerError("reconcile target is not an existing exact record")

        def apply() -> None:
            record.result = envelope
            record.target_request_id = target_id
            record.admission_id = payload["admissionId"]
            record.receipt_id = payload["receiptId"]

        return apply

    def _reconcile_complete(self, reconcile: ReconcileRecord) -> bool:
        if reconcile.result is None:
            return False
        result = reconcile.result.payload["result"]
        target = None if reconcile.target_request_id is None else self._records.get(reconcile.target_request_id)
        if result == "ADMISSION_REJECTED":
            return bool(
                target
                and target.admission
                and target.admission.message_id == reconcile.admission_id
                and target.admission.payload["decision"] == "REJECTED"
            )
        if result == "IN_FLIGHT":
            return bool(
                target
                and target.admission
                and target.admission.message_id == reconcile.admission_id
                and target.admission.payload["decision"] == "ACCEPTED"
                and target.receipt is None
            )
        if result == "KNOWN":
            return bool(
                target
                and target.admission
                and target.receipt
                and target.admission.message_id == reconcile.admission_id
                and target.receipt.message_id == reconcile.receipt_id
            )
        return True

    def _target_record(self, envelope: MinecraftEnvelope) -> ActionRecord | None:
        if envelope.type in {"admission", "receipt"}:
            request_id = envelope.payload["requestId"]
            record = self._records.get(request_id)
            if record is None:
                raise ActionLedgerError("unsolicited request result")
            return record
        return None

    def _validate_origin(self, origin: TrustedServerOrigin) -> None:
        if not isinstance(origin, TrustedServerOrigin) or origin != self._origin:
            self._effect_fence_quarantined = True
            raise ActionLedgerError("authoritative origin mismatch")

    def _validate_outbound(self, envelope: MinecraftEnvelope, expected_type: str) -> None:
        if not isinstance(envelope, MinecraftEnvelope) or envelope.type != expected_type:
            raise ActionLedgerError("invalid outbound message")
        self._validate_exact_envelope_binding(envelope, self._binding)

    def _validate_inbound_binding(self, envelope: MinecraftEnvelope, record: ActionRecord | None):
        binding = self._binding if record is None else record.binding
        try:
            self._validate_exact_envelope_binding(envelope, binding)
            stamp = envelope.payload["stamp"]
            if stamp["serverBootId"] != binding.server_boot_id:
                raise ActionLedgerError("server boot mismatch")
            self._validate_stamp_identity(stamp)
        except ActionLedgerError:
            if record is not None:
                record.quarantined = True
            self._effect_fence_quarantined = True
            raise
        return stamp

    def _validate_stamp_identity(self, stamp) -> None:
        revision = stamp["serverRevision"]
        event_id = stamp["eventId"]
        known_event = self._stamp_events.get(revision)
        known_revision = self._event_revisions.get(event_id)
        if (known_event is not None and known_event != event_id) or (
            known_revision is not None and known_revision != revision
        ):
            raise ActionLedgerError("conflicting server stamp identity")

    @staticmethod
    def _validate_exact_envelope_binding(envelope: MinecraftEnvelope, binding: BindingTuple) -> None:
        if (
            envelope.actor_id != binding.actor_id
            or envelope.world_id != binding.world_id
            or envelope.body_id != binding.body_id
            or envelope.session_id != binding.session_id
            or envelope.session_epoch != binding.session_epoch
        ):
            raise ActionLedgerError("message binding mismatch")

    def _validate_request_time(self, timestamp: datetime) -> None:
        now = self._utc_now()
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() != timedelta(0)
            or timestamp.tzinfo is None
            or timestamp.utcoffset() != timedelta(0)
        ):
            raise ActionLedgerError("invalid trusted UTC clock")
        now, timestamp = now.astimezone(timezone.utc), timestamp.astimezone(timezone.utc)
        if now - timestamp > _REQUEST_PAST_LIMIT or timestamp - now > _REQUEST_FUTURE_LIMIT:
            raise ActionLedgerError("request timestamp is stale")

    def _existing_message_is_duplicate(self, envelope: MinecraftEnvelope, record: ActionRecord | None) -> bool:
        existing = self._messages.get(envelope.message_id)
        if existing is None:
            return False
        if existing == envelope:
            return True
        if record is not None:
            record.quarantined = True
        self._effect_fence_quarantined = True
        raise ActionLedgerError("canonical message identity conflict")

    def _validate_new_message(self, envelope: MinecraftEnvelope, record: ActionRecord | None) -> None:
        if self._existing_message_is_duplicate(envelope, record):
            raise ActionLedgerError("message is already reserved")

    def _commit_message(self, envelope: MinecraftEnvelope, stamp=None) -> None:
        self._messages[envelope.message_id] = envelope
        if stamp is not None:
            self._stamp_events[stamp["serverRevision"]] = stamp["eventId"]
            self._event_revisions[stamp["eventId"]] = stamp["serverRevision"]

    def _reserve_control(self, envelope: MinecraftEnvelope, now: float) -> MinecraftEnvelope:
        existing = self._cancels.get(envelope.message_id)
        if existing is not None:
            if existing.request == envelope:
                return existing.request
            raise ActionLedgerError("control identity conflict")
        if len(self._cancels) >= _MAX_CONTROL_RECORDS:
            raise ActionLedgerError("control record capacity")
        self._validate_new_message(envelope, None)
        self._cancels[envelope.message_id] = _ControlRecord(envelope, now)
        self._commit_message(envelope)
        return envelope

    def _now(self) -> float:
        value = self._monotonic_now()
        if type(value) not in (int, float) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
            raise ActionLedgerError("invalid monotonic clock")
        value = float(value)
        if self._last_monotonic is not None and value < self._last_monotonic:
            self._effect_fence_quarantined = True
            raise ActionLedgerError("monotonic clock rolled back")
        self._last_monotonic = value
        return value

    def _prune(self, now: float) -> None:
        self._refresh_reconcile_terminals(now)
        for reconcile_id, record in tuple(self._reconciles.items()):
            if record.terminal_at is not None and now - record.terminal_at >= _RETENTION_SECONDS:
                self._reconciles.pop(reconcile_id, None)
        referenced_requests = {
            record.target_request_id for record in self._reconciles.values() if record.target_request_id is not None
        }
        for request_id, record in tuple(self._records.items()):
            if (
                record.terminal_at is not None
                and now - record.terminal_at >= _RETENTION_SECONDS
                and request_id not in referenced_requests
            ):
                self._records.pop(request_id, None)
        for cancel_id, record in tuple(self._cancels.items()):
            if now - record.created_at >= _RETENTION_SECONDS:
                self._cancels.pop(cancel_id, None)
        self._rebuild_metadata()

    def _refresh_reconcile_terminals(self, now: float) -> None:
        for record in self._reconciles.values():
            if record.terminal_at is not None:
                continue
            if self._reconcile_complete(record):
                record.terminal_at = now
            elif now - record.created_at >= 5.0:
                record.uncertain = True
                record.terminal_at = now

    def _rebuild_metadata(self) -> None:
        messages: dict[str, MinecraftEnvelope] = {}
        authoritative: list[MinecraftEnvelope] = []
        for record in self._records.values():
            for envelope in (record.request, record.admission, record.receipt, record.buffered_receipt):
                if envelope is not None:
                    messages[envelope.message_id] = envelope
                    if envelope.type in {"admission", "receipt"}:
                        authoritative.append(envelope)
        for record in self._reconciles.values():
            messages[record.request.message_id] = record.request
            if record.result is not None:
                messages[record.result.message_id] = record.result
                authoritative.append(record.result)
        for record in self._cancels.values():
            messages[record.request.message_id] = record.request
            if record.result is not None:
                messages[record.result.message_id] = record.result
                authoritative.append(record.result)
        self._messages = messages
        self._stamp_events = {
            envelope.payload["stamp"]["serverRevision"]: envelope.payload["stamp"]["eventId"]
            for envelope in authoritative
        }
        self._event_revisions = {
            envelope.payload["stamp"]["eventId"]: envelope.payload["stamp"]["serverRevision"]
            for envelope in authoritative
        }

    def _maintain(self) -> float:
        now = self._now()
        self._prune(now)
        return now

    def _terminal(self, record: ActionRecord, now: float) -> None:
        record.phase = ActionRecordPhase.TERMINAL
        record.terminal_at = now

    def _violation(self, record: ActionRecord, message: str) -> None:
        record.quarantined = True
        self._effect_fence_quarantined = True
        raise ActionLedgerError(message)

    def _require_record(self, request_id: str) -> ActionRecord:
        record = self._records.get(request_id)
        if record is None:
            raise ActionLedgerError("unknown request")
        return record

    def _buffered_receipt_count(self) -> int:
        return sum(record.buffered_receipt is not None for record in self._records.values())


def _nonblank(value: object, description: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ActionLedgerError(f"invalid {description}")
