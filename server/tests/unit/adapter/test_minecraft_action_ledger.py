import json
from datetime import datetime, timezone

import pytest

from src.adapter.minecraft.action_ledger import (
    ActionLedgerError,
    ActionRecordPhase,
    MinecraftActionLedger,
    TrustedServerOrigin,
)
from src.adapter.minecraft.control_auth import AuthenticatedHandshake, ControlAuthContext
from src.adapter.minecraft.control_binding import AuthenticatedRoleConnection, BindingTuple
from src.adapter.minecraft.wire import decode_message
from src.domain.agent.minecraft import MinecraftPortAdmission

NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)
ACTOR = "actor"
WORLD = "world"
BODY = "10000000-0000-0000-0000-000000000001"
SESSION = "20000000-0000-0000-0000-000000000002"
BOOT = "30000000-0000-0000-0000-000000000003"
GAME = "40000000-0000-0000-0000-000000000004"
REQUEST = "50000000-0000-0000-0000-000000000005"
ADMISSION = "60000000-0000-0000-0000-000000000006"
RECEIPT = "70000000-0000-0000-0000-000000000007"
EVIDENCE = "80000000-0000-0000-0000-000000000008"
OBSERVATION = "90000000-0000-0000-0000-000000000009"


class Clock:
    def __init__(self):
        self.value = 10.0

    def __call__(self):
        return self.value


def server_origin(connection_id="server-connection", install="server-install"):
    handshake = AuthenticatedHandshake(
        context=ControlAuthContext(
            peer_role="SERVER_AUTHORITY",
            peer_install_id=install,
            hub_install_id="hub",
            key_id="key",
            protocol="luosandbox.minecraft.v1",
            mod_hash=b"m" * 32,
            config_hash=b"c" * 32,
            binding_hash=b"b" * 32,
        ),
        challenge_id="a0000000-0000-0000-0000-00000000000a",
        ready_proof="proof",
    )
    return TrustedServerOrigin(
        connection=AuthenticatedRoleConnection(connection_id=connection_id, handshake=handshake),
        expected_install_id="server-install",
    )


def binding():
    return BindingTuple(
        actor_id=ACTOR,
        world_id=WORLD,
        body_id=BODY,
        session_id=SESSION,
        session_epoch=1,
        server_boot_id=BOOT,
        game_connection_id=GAME,
    )


def envelope(kind, message_id, payload, **changes):
    value = {
        "protocol": "luosandbox.minecraft.v1",
        "type": kind,
        "actorId": ACTOR,
        "worldId": WORLD,
        "bodyId": BODY,
        "sessionId": SESSION,
        "sessionEpoch": 1,
        "messageId": message_id,
        "timestamp": "2026-10-23T00:00:00.000Z",
        "payload": payload,
    }
    value.update(changes)
    return decode_message(json.dumps(value, separators=(",", ":")).encode())


def stamp(revision=1, evidence=()):
    return {
        "serverBootId": BOOT,
        "serverRevision": revision,
        "serverTick": revision,
        "eventId": f"b0000000-0000-0000-0000-{revision:012x}",
        "evidence": list(evidence),
    }


def request(request_id=REQUEST, intent="PUBLIC_SAY", timestamp=None):
    intents = {
        "QUERY": {"kind": "QUERY", "queryKind": "READ_ONLY_SUMMARY"},
        "PUBLIC_SAY": {"kind": "PUBLIC_SAY", "message": "hello"},
        "LOOK": {
            "kind": "LOOK",
            "dimensionId": "minecraft:overworld",
            "target": {"x": 1, "y": 64, "z": 2},
        },
        "BOUNDED_WALK": {
            "kind": "BOUNDED_WALK",
            "dimensionId": "minecraft:overworld",
            "target": {"x": 2, "y": 64, "z": 2},
            "maximumDistance": 1,
        },
    }
    basis = None if intent == "QUERY" else {"observationId": OBSERVATION, "serverBootId": BOOT, "serverRevision": 1}
    return envelope(
        "request",
        request_id,
        {"deadline": "2026-10-23T00:00:05.000Z", "basis": basis, "intent": intents[intent]},
        **({} if timestamp is None else {"timestamp": timestamp}),
    )


def admission(decision="ACCEPTED", message_id=ADMISSION, request_id=REQUEST, reason=None, revision=2, **changes):
    return envelope(
        "admission",
        message_id,
        {"requestId": request_id, "decision": decision, "reason": reason, "stamp": stamp(revision)},
        **changes,
    )


def receipt(
    status="SUCCEEDED",
    kind="PUBLIC_BROADCAST",
    message_id=RECEIPT,
    request_id=REQUEST,
    revision=3,
    **changes,
):
    reasons = {
        "SUCCEEDED": None,
        "UNKNOWN": "OUTCOME_UNPROVEN",
        "CANCELLED": "CANCEL_CONFIRMED",
    }
    effects = {"SUCCEEDED": "COMPLETE", "UNKNOWN": "UNKNOWN", "CANCELLED": "NONE"}
    if status == "UNKNOWN":
        kind = "UNDETERMINED"
    state = None
    observation_id = None
    if kind not in {"UNDETERMINED", "PUBLIC_BROADCAST"}:
        state = {
            "dimensionId": "minecraft:overworld",
            "position": {"x": 1, "y": 64, "z": 2},
            "yaw": 0,
            "pitch": 0,
        }
        observation_id = OBSERVATION
    return envelope(
        "receipt",
        message_id,
        {
            "requestId": request_id,
            "status": status,
            "reason": reasons[status],
            "stamp": stamp(revision, () if status == "UNKNOWN" else (EVIDENCE,)),
            "outcome": {"kind": kind, "effect": effects[status], "state": state, "observationId": observation_id},
        },
        **changes,
    )


def ledger(clock=None):
    origin = server_origin()
    return (
        MinecraftActionLedger(
            binding=binding(),
            server_origin=origin,
            monotonic_now=clock or Clock(),
            utc_now=lambda: NOW,
        ),
        origin,
    )


def reconcile(reconcile_id, target=REQUEST, epoch=1):
    return envelope(
        "reconcile",
        reconcile_id,
        {"targetRequestId": target, "targetEpoch": epoch, "deadline": "2026-10-23T00:00:05.000Z"},
    )


def reconcile_result(reconcile_id, result, *, admission_id=None, receipt_id=None, target=REQUEST):
    return envelope(
        "reconcile_result",
        "c0000000-0000-0000-0000-00000000000c",
        {
            "reconcileId": reconcile_id,
            "targetRequestId": target,
            "targetEpoch": 1,
            "result": result,
            "reason": None,
            "quiescent": False,
            "observationId": None,
            "admissionId": admission_id,
            "receiptId": receipt_id,
            "stamp": stamp(9),
        },
    )


def cancel(cancel_id, target=REQUEST):
    return envelope(
        "cancel",
        cancel_id,
        {"mode": "TARGET", "targetRequestId": target, "targetEpoch": 1, "deadline": "2026-10-23T00:00:05.000Z"},
    )


def cancel_result(cancel_id, *, message_id="16000000-0000-0000-0000-000000000016", revision=5):
    return envelope(
        "cancel_result",
        message_id,
        {
            "cancelId": cancel_id,
            "mode": "TARGET",
            "targetRequestId": REQUEST,
            "targetEpoch": 1,
            "result": "NOT_FOUND",
            "reason": "TARGET_NOT_FOUND",
            "stamp": stamp(revision),
        },
    )


def test_accepted_then_success_and_rejected_without_receipt_are_independent():
    owner, origin = ledger()
    owner.reserve_request(request(), local_action_id="action-1")
    assert owner.dispatch_admission(REQUEST) is MinecraftPortAdmission.UNKNOWN
    owner.ingest(admission(), origin=origin)
    assert owner.dispatch_admission(REQUEST) is MinecraftPortAdmission.ACCEPTED
    owner.ingest(receipt(), origin=origin)
    assert owner.record(REQUEST).phase is ActionRecordPhase.TERMINAL
    rejected_id = "d0000000-0000-0000-0000-00000000000d"
    owner.reserve_request(request(rejected_id), local_action_id="action-2")
    owner.ingest(
        admission(
            decision="REJECTED",
            message_id="e0000000-0000-0000-0000-00000000000e",
            request_id=rejected_id,
            reason="UNSAFE_TARGET",
            revision=4,
        ),
        origin=origin,
    )
    assert owner.dispatch_admission(rejected_id) is MinecraftPortAdmission.DECLINED
    assert owner.record(rejected_id).receipt is None


@pytest.mark.parametrize("field", ["actorId", "worldId", "bodyId", "sessionId", "sessionEpoch"])
def test_wrong_binding_and_wrong_server_origin_quarantine_without_mutating_known_result(field):
    owner, origin = ledger()
    owner.reserve_request(request(), local_action_id="action")
    changes = {
        "actorId": "other",
        "worldId": "other",
        "bodyId": "f0000000-0000-0000-0000-00000000000f",
        "sessionId": "11000000-0000-0000-0000-000000000011",
        "sessionEpoch": 2,
    }
    with pytest.raises(ActionLedgerError):
        owner.ingest(admission(**{field: changes[field]}), origin=origin)
    assert owner.record(REQUEST).admission is None and owner.effect_fence_quarantined
    owner2, _ = ledger()
    owner2.reserve_request(request(), local_action_id="action")
    with pytest.raises(ActionLedgerError):
        owner2.ingest(admission(), origin=server_origin(connection_id="other"))


def test_receipt_before_admission_joins_bounded_and_success_kind_mismatch_quarantines():
    owner, origin = ledger()
    owner.reserve_request(request(), local_action_id="action")
    owner.ingest(receipt(), origin=origin)
    assert owner.record(REQUEST).buffered_receipt is not None
    owner.ingest(admission(), origin=origin)
    assert owner.record(REQUEST).receipt is not None
    owner2, origin2 = ledger()
    owner2.reserve_request(request(), local_action_id="action")
    owner2.ingest(admission(), origin=origin2)
    with pytest.raises(ActionLedgerError, match="outcome"):
        owner2.ingest(receipt(kind="POSITION"), origin=origin2)
    assert owner2.record(REQUEST).receipt is None and owner2.record(REQUEST).quarantined


def test_identical_duplicate_is_idempotent_but_conflict_does_not_replace_canonical_values():
    owner, origin = ledger()
    owner.reserve_request(request(), local_action_id="action")
    accepted = admission()
    owner.ingest(accepted, origin=origin)
    owner.ingest(accepted, origin=origin)
    with pytest.raises(ActionLedgerError):
        owner.ingest(admission(decision="REJECTED", message_id=ADMISSION, reason="UNSAFE_TARGET"), origin=origin)
    assert owner.record(REQUEST).admission == accepted


def test_unknown_receipt_is_immutable_terminal_and_known_reconcile_never_clears_fence():
    owner, origin = ledger()
    owner.reserve_request(request(), local_action_id="action")
    owner.ingest(admission(), origin=origin)
    unknown = receipt(status="UNKNOWN")
    owner.ingest(unknown, origin=origin)
    with pytest.raises(ActionLedgerError):
        owner.ingest(receipt(message_id="12000000-0000-0000-0000-000000000012"), origin=origin)
    reconcile_id = "13000000-0000-0000-0000-000000000013"
    owner.reserve_reconcile(reconcile(reconcile_id))
    owner.ingest(
        reconcile_result(reconcile_id, "KNOWN", admission_id=ADMISSION, receipt_id=RECEIPT),
        origin=origin,
    )
    view = owner.reconcile_view(reconcile_id)
    assert view.complete and view.fence_cleared is False and owner.effect_fence_quarantined
    assert owner.record(REQUEST).receipt == unknown


@pytest.mark.parametrize("result", ["IN_FLIGHT", "ADMISSION_REJECTED", "KNOWN"])
def test_reconcile_requires_actual_replayed_messages_in_any_order(result):
    owner, origin = ledger()
    owner.reserve_request(request(), local_action_id="action")
    reconcile_id = "14000000-0000-0000-0000-000000000014"
    owner.reserve_reconcile(reconcile(reconcile_id))
    admission_id = ADMISSION
    receipt_id = RECEIPT if result == "KNOWN" else None
    owner.ingest(
        reconcile_result(reconcile_id, result, admission_id=admission_id, receipt_id=receipt_id), origin=origin
    )
    assert owner.reconcile_view(reconcile_id).complete is False
    decision = "REJECTED" if result == "ADMISSION_REJECTED" else "ACCEPTED"
    owner.ingest(
        admission(decision=decision, reason="UNSAFE_TARGET" if decision == "REJECTED" else None),
        origin=origin,
    )
    if result == "KNOWN":
        assert owner.reconcile_view(reconcile_id).complete is False
        owner.ingest(receipt(status="UNKNOWN"), origin=origin)
    assert owner.reconcile_view(reconcile_id).complete is True


def test_cancel_ack_does_not_change_target_terminal_result():
    owner, origin = ledger()
    owner.reserve_request(request(), local_action_id="action")
    owner.ingest(admission(), origin=origin)
    owner.ingest(receipt(), origin=origin)
    cancel_id = "15000000-0000-0000-0000-000000000015"
    cancel = envelope(
        "cancel",
        cancel_id,
        {"mode": "TARGET", "targetRequestId": REQUEST, "targetEpoch": 1, "deadline": "2026-10-23T00:00:05.000Z"},
    )
    owner.reserve_cancel(cancel)
    result = envelope(
        "cancel_result",
        "16000000-0000-0000-0000-000000000016",
        {
            "cancelId": cancel_id,
            "mode": "TARGET",
            "targetRequestId": REQUEST,
            "targetEpoch": 1,
            "result": "ALREADY_TERMINAL",
            "reason": "TARGET_TERMINAL",
            "stamp": stamp(5),
        },
    )
    owner.ingest(result, origin=origin)
    assert owner.record(REQUEST).receipt.payload["status"] == "SUCCEEDED"


def test_active_records_are_not_evicted_capacity_and_expired_terminal_ids_cannot_reexecute_stale_request():
    clock = Clock()
    owner, origin = ledger(clock)
    for index in range(32):
        request_id = f"17000000-0000-0000-0000-{index:012x}"
        owner.reserve_request(request(request_id), local_action_id=f"action-{index}")
    with pytest.raises(ActionLedgerError, match="capacity"):
        owner.reserve_request(request("18000000-0000-0000-0000-000000000018"), local_action_id="overflow")
    owner2, origin2 = ledger(clock)
    owner2.reserve_request(request(), local_action_id="action")
    owner2.ingest(admission(), origin=origin2)
    owner2.ingest(receipt(), origin=origin2)
    clock.value += 300
    stale = envelope(
        "request",
        REQUEST,
        {
            "deadline": "2026-10-22T23:59:55.000Z",
            "basis": {"observationId": OBSERVATION, "serverBootId": BOOT, "serverRevision": 1},
            "intent": {"kind": "PUBLIC_SAY", "message": "hello"},
        },
        timestamp="2026-10-22T23:59:50.000Z",
    )
    with pytest.raises(ActionLedgerError, match="stale"):
        owner2.reserve_request(stale, local_action_id="new-action")


def test_send_unknown_preserves_reference_quarantines_and_never_retries():
    owner, _ = ledger()
    original = owner.reserve_request(request(), local_action_id="action")
    requirement = owner.mark_send_unknown(REQUEST)
    assert owner.record(REQUEST) is original
    assert original.phase is ActionRecordPhase.UNCERTAIN
    assert requirement.binding == binding() and owner.effect_fence_quarantined
    assert owner.reserve_request(request(), local_action_id="action") is original


def test_completed_reconcile_retention_frees_eight_slots_and_metadata():
    clock = Clock()
    owner, origin = ledger(clock)
    owner.reserve_request(request(), local_action_id="action")
    owner.ingest(admission(), origin=origin)
    for index in range(8):
        reconcile_id = f"21000000-0000-0000-0000-{index:012x}"
        owner.reserve_reconcile(reconcile(reconcile_id))
        result = reconcile_result(reconcile_id, "IN_FLIGHT", admission_id=ADMISSION)
        result = envelope(
            result.type,
            f"22000000-0000-0000-0000-{index:012x}",
            dict(result.payload) | {"stamp": stamp(20 + index)},
        )
        owner.ingest(result, origin=origin)
        assert owner.reconcile_view(reconcile_id).complete
    with pytest.raises(ActionLedgerError, match="capacity"):
        owner.reserve_reconcile(reconcile("23000000-0000-0000-0000-000000000023"))
    clock.value += 300
    owner.reserve_reconcile(reconcile("23000000-0000-0000-0000-000000000023"))
    counts = owner.metadata_counts
    assert counts["reconciles"] == 1
    assert counts["messages"] <= 4
    assert counts["stamp_events"] <= 1


def test_cancel_retention_frees_sixty_four_slots_and_repeated_cycles_stay_bounded():
    clock = Clock()
    owner, origin = ledger(clock)
    owner.reserve_request(request(), local_action_id="action")
    for cycle in range(3):
        for index in range(64):
            cancel_id = f"24000000-0000-0000-{cycle:04x}-{index:012x}"
            owner.reserve_cancel(cancel(cancel_id))
            result = cancel_result(
                cancel_id,
                message_id=f"25000000-0000-0000-{cycle:04x}-{index:012x}",
                revision=100 + cycle * 64 + index,
            )
            owner.ingest(result, origin=origin)
        with pytest.raises(ActionLedgerError, match="capacity"):
            owner.reserve_cancel(cancel(f"26000000-0000-0000-{cycle:04x}-000000000000"))
        assert owner.metadata_counts["cancels"] == 64
        clock.value += 300
        owner.dispatch_admission(REQUEST)
        counts = owner.metadata_counts
        assert counts["cancels"] == 0
        assert counts["messages"] == 1
        assert counts["stamp_events"] == counts["event_revisions"] == 0


def test_unfinished_reconcile_expires_uncertain_but_keeps_referenced_active_action_until_retention():
    clock = Clock()
    owner, _ = ledger(clock)
    action = owner.reserve_request(request(), local_action_id="action")
    reconcile_id = "27000000-0000-0000-0000-000000000027"
    owner.reserve_reconcile(reconcile(reconcile_id))
    clock.value += 5
    view = owner.reconcile_view(reconcile_id)
    assert view.complete is False
    assert owner.record(REQUEST) is action
    assert owner.metadata_counts["reconciles"] == 1
    clock.value += 300
    with pytest.raises(ActionLedgerError, match="unknown reconcile"):
        owner.reconcile_view(reconcile_id)
    assert owner.record(REQUEST) is action


def test_receipt_join_capacity_rejection_is_not_remembered_and_replay_can_join_after_capacity_frees():
    owner, origin = ledger()
    request_ids = [f"28000000-0000-0000-0000-{index:012x}" for index in range(9)]
    for request_id in request_ids:
        owner.reserve_request(request(request_id), local_action_id=request_id)
    receipts = []
    for index, request_id in enumerate(request_ids):
        item = receipt(
            message_id=f"29000000-0000-0000-0000-{index:012x}",
            request_id=request_id,
            revision=300 + index,
        )
        receipts.append(item)
    for item in receipts[:8]:
        owner.ingest(item, origin=origin)
    before = owner.metadata_counts.copy()
    with pytest.raises(ActionLedgerError, match="join capacity"):
        owner.ingest(receipts[8], origin=origin)
    assert owner.metadata_counts == before
    first_admission = admission(
        request_id=request_ids[0],
        message_id="2a000000-0000-0000-0000-00000000002a",
        revision=400,
    )
    owner.ingest(first_admission, origin=origin)
    owner.ingest(receipts[8], origin=origin)
    assert owner.record(request_ids[8]).buffered_receipt == receipts[8]


def test_semantically_invalid_messages_do_not_poison_dedup_or_stamp_provenance():
    owner, origin = ledger()
    owner.reserve_request(request(), local_action_id="action")
    owner.ingest(admission(), origin=origin)
    invalid = receipt(kind="POSITION", revision=500)
    before = owner.metadata_counts.copy()
    for _ in range(2):
        with pytest.raises(ActionLedgerError, match="outcome"):
            owner.ingest(invalid, origin=origin)
        assert owner.metadata_counts == before
        assert owner.record(REQUEST).receipt is None
    valid = receipt(revision=500)
    owner.ingest(valid, origin=origin)
    assert owner.record(REQUEST).receipt == valid


def test_rejected_admission_with_buffered_receipt_and_bad_reconcile_repeat_reject_without_partial_state():
    owner, origin = ledger()
    owner.reserve_request(request(), local_action_id="action")
    buffered = receipt(revision=600)
    owner.ingest(buffered, origin=origin)
    rejected = admission(decision="REJECTED", reason="UNSAFE_TARGET", revision=601)
    counts = owner.metadata_counts.copy()
    for _ in range(2):
        with pytest.raises(ActionLedgerError, match="rejected request"):
            owner.ingest(rejected, origin=origin)
        assert owner.record(REQUEST).admission is None
        assert owner.metadata_counts == counts
    reconcile_id = "2b000000-0000-0000-0000-00000000002b"
    owner.reserve_reconcile(reconcile(reconcile_id))
    wrong = reconcile_result(reconcile_id, "IN_FLIGHT", admission_id=ADMISSION)
    wrong = envelope(
        wrong.type,
        wrong.message_id,
        {
            "reconcileId": wrong.payload["reconcileId"],
            "targetRequestId": wrong.payload["targetRequestId"],
            "targetEpoch": 2,
            "result": wrong.payload["result"],
            "reason": wrong.payload["reason"],
            "quiescent": wrong.payload["quiescent"],
            "observationId": wrong.payload["observationId"],
            "admissionId": wrong.payload["admissionId"],
            "receiptId": wrong.payload["receiptId"],
            "stamp": stamp(700),
        },
    )
    counts = owner.metadata_counts.copy()
    for _ in range(2):
        with pytest.raises(ActionLedgerError, match="original reconcile"):
            owner.ingest(wrong, origin=origin)
        assert owner.metadata_counts == counts
        assert owner.reconcile_view(reconcile_id).result is None


def test_time_rollback_during_metadata_maintenance_fails_closed_without_pruning_references():
    clock = Clock()
    owner, _ = ledger(clock)
    action = owner.reserve_request(request(), local_action_id="action")
    owner.reserve_reconcile(reconcile("2c000000-0000-0000-0000-00000000002c"))
    counts = owner.metadata_counts.copy()
    clock.value = 9
    with pytest.raises(ActionLedgerError, match="rolled back"):
        owner.dispatch_admission(REQUEST)
    assert owner.effect_fence_quarantined
    assert owner._records[REQUEST] is action
    assert owner.metadata_counts == counts
