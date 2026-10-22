import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from src.adapter.minecraft.action_ledger import MinecraftActionLedger, TrustedServerOrigin
from src.adapter.minecraft.action_port import ActiveActionContext, OfflineMinecraftActionPort, TrustedActionObservation
from src.adapter.minecraft.control_auth import AuthenticatedHandshake, ControlAuthContext
from src.adapter.minecraft.control_binding import AuthenticatedRoleConnection, BindingTuple
from src.adapter.minecraft.wire import decode_message
from src.domain.agent.minecraft import (
    MinecraftActionRequest,
    MinecraftBodyState,
    MinecraftBoundScope,
    MinecraftCapabilities,
    MinecraftCapabilityAvailability,
    MinecraftDispatchResult,
    MinecraftIntent,
    MinecraftPortAdmission,
    MinecraftPosition,
)

NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)
BODY = "10000000-0000-0000-0000-000000000001"
SESSION = "20000000-0000-0000-0000-000000000002"
BOOT = "30000000-0000-0000-0000-000000000003"
GAME = "40000000-0000-0000-0000-000000000004"
OBSERVATION = "50000000-0000-0000-0000-000000000005"


class Ids:
    def __init__(self):
        self.value = 10

    def __call__(self):
        value = f"60000000-0000-0000-0000-{self.value:012x}"
        self.value += 1
        return value


class Sender:
    def __init__(self):
        self.sent, self.revoked = [], []
        self.on_send = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def send(self, envelope):
        self.sent.append(envelope)
        self.entered.set()
        if self.on_send is not None:
            await self.on_send(envelope)

    async def revoke(self, requirement):
        self.revoked.append(requirement)


class MonoClock:
    def __init__(self):
        self.value = 10.0

    def __call__(self):
        return self.value


def origin(connection="server-conn"):
    auth = AuthenticatedHandshake(
        context=ControlAuthContext(
            peer_role="SERVER_AUTHORITY",
            peer_install_id="server-install",
            hub_install_id="hub",
            key_id="key",
            protocol="luosandbox.minecraft.v1",
            mod_hash=b"m" * 32,
            config_hash=b"c" * 32,
            binding_hash=b"b" * 32,
        ),
        challenge_id="70000000-0000-0000-0000-000000000007",
        ready_proof="proof",
    )
    return TrustedServerOrigin(
        connection=AuthenticatedRoleConnection(connection_id=connection, handshake=auth),
        expected_install_id="server-install",
    )


def binding(session=SESSION, epoch=1):
    return BindingTuple(
        actor_id="actor",
        world_id="world",
        body_id=BODY,
        session_id=session,
        session_epoch=epoch,
        server_boot_id=BOOT,
        game_connection_id=GAME,
    )


def capabilities(**changes):
    values = dict(
        query=MinecraftCapabilityAvailability.AVAILABLE,
        public_say=MinecraftCapabilityAvailability.AVAILABLE,
        look=MinecraftCapabilityAvailability.AVAILABLE,
        bounded_walk=MinecraftCapabilityAvailability.AVAILABLE,
        stop=MinecraftCapabilityAvailability.AVAILABLE,
    )
    values.update(changes)
    return MinecraftCapabilities(**values)


def context(*, selected_binding=None, selected_origin=None, observed_at=NOW, caps=None):
    return ActiveActionContext(
        binding=selected_binding or binding(),
        server_origin=selected_origin or origin(),
        observation=TrustedActionObservation(
            observation_id=OBSERVATION,
            server_boot_id=BOOT,
            server_revision=7,
            observed_at=observed_at,
            capabilities=caps or capabilities(),
        ),
    )


def scope(selected=None):
    selected = selected or binding()
    return MinecraftBoundScope(
        world_id=selected.world_id, body_id=selected.body_id, session_epoch=selected.session_epoch
    )


def request(intent, local_id, **changes):
    values = dict(
        request_id=local_id,
        scope=scope(),
        intent=intent,
        basis_observation_id=None if intent is MinecraftIntent.QUERY else OBSERVATION,
        public_message="hello" if intent is MinecraftIntent.PUBLIC_SAY else None,
        target=(
            MinecraftBodyState(
                dimension_id="minecraft:overworld",
                position=MinecraftPosition(x=1.0, y=64.0, z=2.0),
                yaw=0.0,
                pitch=0.0,
            )
            if intent in {MinecraftIntent.LOOK, MinecraftIntent.BOUNDED_WALK}
            else None
        ),
        maximum_distance=1.0 if intent is MinecraftIntent.BOUNDED_WALK else None,
        target_request_id=None,
    )
    values.update(changes)
    return MinecraftActionRequest(**values)


def authoritative(kind, message_id, request_id, decision="ACCEPTED", *, selected_binding=None, selected_origin=None):
    selected_binding = selected_binding or binding()
    payload = {
        "requestId": request_id,
        "decision": decision,
        "reason": None if decision == "ACCEPTED" else "UNSAFE_TARGET",
        "stamp": {
            "serverBootId": selected_binding.server_boot_id,
            "serverRevision": 10,
            "serverTick": 10,
            "eventId": "80000000-0000-0000-0000-000000000008",
            "evidence": [],
        },
    }
    value = {
        "protocol": "luosandbox.minecraft.v1",
        "type": kind,
        "actorId": selected_binding.actor_id,
        "worldId": selected_binding.world_id,
        "bodyId": selected_binding.body_id,
        "sessionId": selected_binding.session_id,
        "sessionEpoch": selected_binding.session_epoch,
        "messageId": message_id,
        "timestamp": "2026-10-23T00:00:00.000Z",
        "payload": payload,
    }
    return decode_message(json.dumps(value, separators=(",", ":")).encode())


def make_port(*, sender=None, active=None, timeout=0.05, monotonic=None):
    sender = sender or Sender()
    selected = active or context()
    holder = {"value": selected}
    monotonic = monotonic or MonoClock()
    ledger = MinecraftActionLedger(
        binding=selected.binding,
        server_origin=selected.server_origin,
        monotonic_now=monotonic,
        utc_now=lambda: NOW,
    )
    port = OfflineMinecraftActionPort(
        ledger=ledger,
        sender=sender,
        active_context=lambda: holder["value"],
        monotonic_now=monotonic,
        utc_now=lambda: NOW,
        admission_timeout_seconds=timeout,
        message_id=Ids(),
    )
    return port, ledger, sender, holder


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "intent", [MinecraftIntent.QUERY, MinecraftIntent.PUBLIC_SAY, MinecraftIntent.LOOK, MinecraftIntent.BOUNDED_WALK]
)
async def test_four_requests_use_canonical_trusted_binding_and_only_server_admission_accepts(intent):
    port, ledger, sender, holder = make_port()

    async def respond(envelope):
        port.receive_authoritative(
            authoritative("admission", "90000000-0000-0000-0000-000000000009", envelope.message_id),
            origin=holder["value"].server_origin,
        )

    sender.on_send = respond
    local_id = f"local-{intent.value}"
    result = await port.submit(request(intent, local_id))
    outbound = sender.sent[0]
    assert result == MinecraftDispatchResult(request_id=local_id, admission=MinecraftPortAdmission.ACCEPTED)
    assert (outbound.actor_id, outbound.world_id, outbound.body_id) == ("actor", "world", BODY)
    assert (outbound.session_id, outbound.session_epoch) == (SESSION, 1)
    assert outbound.payload["intent"]["kind"] == intent.value
    assert ledger.record(outbound.message_id).local_action_id == local_id


@pytest.mark.asyncio
async def test_send_completion_is_not_admission_rejection_and_timeout_are_distinct():
    port, _, sender, holder = make_port()
    unknown = await port.submit(request(MinecraftIntent.QUERY, "unknown"))
    assert unknown.admission is MinecraftPortAdmission.UNKNOWN and sender.revoked
    port, _, sender, holder = make_port()

    async def reject(envelope):
        port.receive_authoritative(
            authoritative(
                "admission", "a0000000-0000-0000-0000-00000000000a", envelope.message_id, decision="REJECTED"
            ),
            origin=holder["value"].server_origin,
        )

    sender.on_send = reject
    declined = await port.submit(request(MinecraftIntent.QUERY, "declined"))
    assert declined.admission is MinecraftPortAdmission.DECLINED


@pytest.mark.asyncio
async def test_stale_basis_unavailable_capability_and_replaced_context_do_not_send():
    stale = context(observed_at=NOW - timedelta(seconds=2))
    port, _, sender, _ = make_port(active=stale)
    with pytest.raises(ValueError, match="stale"):
        await port.submit(request(MinecraftIntent.PUBLIC_SAY, "stale"))
    assert sender.sent == []
    unavailable = context(caps=capabilities(look=MinecraftCapabilityAvailability.UNKNOWN))
    port, _, sender, _ = make_port(active=unavailable)
    with pytest.raises(ValueError, match="unavailable"):
        await port.submit(request(MinecraftIntent.LOOK, "unavailable"))
    assert sender.sent == []
    port, ledger, sender, holder = make_port()

    async def replace(_):
        holder["value"] = context(selected_binding=binding(session="b0000000-0000-0000-0000-00000000000b", epoch=2))

    sender.on_send = replace
    result = await port.submit(request(MinecraftIntent.QUERY, "replace"))
    assert result.admission is MinecraftPortAdmission.UNKNOWN and ledger.effect_fence_quarantined


@pytest.mark.asyncio
async def test_wrong_origin_and_receipt_before_admission_do_not_complete_waiter():
    port, ledger, sender, holder = make_port()

    async def respond(envelope):
        with pytest.raises(Exception):
            port.receive_authoritative(
                authoritative("admission", "c0000000-0000-0000-0000-00000000000c", envelope.message_id),
                origin=origin(connection="wrong"),
            )
        receipt_value = {
            "protocol": "luosandbox.minecraft.v1",
            "type": "receipt",
            "actorId": "actor",
            "worldId": "world",
            "bodyId": BODY,
            "sessionId": SESSION,
            "sessionEpoch": 1,
            "messageId": "d0000000-0000-0000-0000-00000000000d",
            "timestamp": "2026-10-23T00:00:00.000Z",
            "payload": {
                "requestId": envelope.message_id,
                "status": "UNKNOWN",
                "reason": "OUTCOME_UNPROVEN",
                "stamp": {
                    "serverBootId": BOOT,
                    "serverRevision": 11,
                    "serverTick": 11,
                    "eventId": "e0000000-0000-0000-0000-00000000000e",
                    "evidence": [],
                },
                "outcome": {"kind": "UNDETERMINED", "effect": "UNKNOWN", "state": None, "observationId": None},
            },
        }
        port.receive_authoritative(
            decode_message(json.dumps(receipt_value, separators=(",", ":")).encode()),
            origin=holder["value"].server_origin,
        )

    sender.on_send = respond
    result = await port.submit(request(MinecraftIntent.QUERY, "waiting"))
    assert result.admission is MinecraftPortAdmission.UNKNOWN
    assert ledger.record(sender.sent[0].message_id).receipt is None


@pytest.mark.asyncio
async def test_stop_builds_target_cancel_for_existing_same_context_request():
    port, _, sender, holder = make_port()
    admission_ids = iter(["f0000000-0000-0000-0000-00000000000f", "11000000-0000-0000-0000-000000000011"])

    async def respond(envelope):
        if envelope.type == "request":
            port.receive_authoritative(
                authoritative("admission", next(admission_ids), envelope.message_id),
                origin=holder["value"].server_origin,
            )
        else:
            value = {
                "protocol": envelope.protocol,
                "type": "cancel_result",
                "actorId": envelope.actor_id,
                "worldId": envelope.world_id,
                "bodyId": envelope.body_id,
                "sessionId": envelope.session_id,
                "sessionEpoch": envelope.session_epoch,
                "messageId": next(admission_ids),
                "timestamp": "2026-10-23T00:00:00.000Z",
                "payload": {
                    "cancelId": envelope.message_id,
                    "mode": "TARGET",
                    "targetRequestId": envelope.payload["targetRequestId"],
                    "targetEpoch": 1,
                    "result": "REQUESTED",
                    "reason": None,
                    "stamp": {
                        "serverBootId": BOOT,
                        "serverRevision": 12,
                        "serverTick": 12,
                        "eventId": "12000000-0000-0000-0000-000000000012",
                        "evidence": [],
                    },
                },
            }
            port.receive_authoritative(
                decode_message(json.dumps(value, separators=(",", ":")).encode()),
                origin=holder["value"].server_origin,
            )

    sender.on_send = respond
    await port.submit(request(MinecraftIntent.QUERY, "target"))
    result = await port.cancel(
        request(
            MinecraftIntent.STOP,
            "stop",
            basis_observation_id=None,
            target_request_id="target",
        )
    )
    cancel = sender.sent[-1]
    assert result.admission is MinecraftPortAdmission.ACCEPTED
    assert cancel.type == "cancel" and cancel.payload["targetRequestId"] == sender.sent[0].message_id


@pytest.mark.asyncio
async def test_send_cancellation_and_close_fence_without_pending_task_leaks():
    port, ledger, sender, _ = make_port(timeout=5)

    async def block(_):
        await sender.release.wait()

    sender.on_send = block
    task = asyncio.create_task(port.submit(request(MinecraftIntent.QUERY, "cancelled")))
    await sender.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert port.pending_count == 0 and ledger.effect_fence_quarantined and sender.revoked
    port, _, sender, _ = make_port(timeout=5)
    task = asyncio.create_task(port.submit(request(MinecraftIntent.QUERY, "closed")))
    await sender.entered.wait()
    await port.close()
    result = await task
    assert result.admission is MinecraftPortAdmission.UNKNOWN
    assert port.pending_count == 0


@pytest.mark.asyncio
async def test_permanently_blocked_send_uses_single_deadline_and_self_terminates_unknown():
    sender = Sender()

    async def block(_):
        await sender.release.wait()

    sender.on_send = block
    port, ledger, _, _ = make_port(sender=sender, timeout=0.02)
    result = await port.submit(request(MinecraftIntent.QUERY, "blocked"))
    assert result.admission is MinecraftPortAdmission.UNKNOWN
    assert port.pending_count == 0 and port.owned_send_task_count == 0
    assert ledger.effect_fence_quarantined and sender.revoked


@pytest.mark.asyncio
async def test_close_during_blocked_send_finishes_submit_without_manual_cancel():
    sender = Sender()

    async def block(_):
        await sender.release.wait()

    sender.on_send = block
    port, ledger, _, _ = make_port(sender=sender, timeout=5)
    task = asyncio.create_task(port.submit(request(MinecraftIntent.QUERY, "close-blocked")))
    await sender.entered.wait()
    await port.close()
    result = await asyncio.wait_for(task, timeout=0.2)
    assert result.admission is MinecraftPortAdmission.UNKNOWN
    assert port.pending_count == port.owned_send_task_count == 0
    assert ledger.effect_fence_quarantined and sender.revoked


@pytest.mark.asyncio
async def test_send_and_admission_share_one_monotonic_budget():
    clock = MonoClock()
    sender = Sender()
    port, ledger, _, _ = make_port(sender=sender, timeout=5, monotonic=clock)

    async def consume_budget(_):
        clock.value = 14.9

    sender.on_send = consume_budget
    task = asyncio.create_task(port.submit(request(MinecraftIntent.QUERY, "budget")))
    await sender.entered.wait()
    clock.value = 15.0
    result = await asyncio.wait_for(task, timeout=0.2)
    assert result.admission is MinecraftPortAdmission.UNKNOWN
    assert ledger.effect_fence_quarantined


@pytest.mark.asyncio
async def test_correlations_follow_ledger_retention_and_expired_stop_declines_without_send():
    clock = MonoClock()
    port, ledger, sender, holder = make_port(timeout=0.01, monotonic=clock)
    for cycle in range(3):
        for index in range(32):
            local_id = f"cycle-{cycle}-{index}"

            async def reject(envelope, response_index=index):
                port.receive_authoritative(
                    authoritative(
                        "admission",
                        f"d0000000-0000-{cycle:04x}-{response_index:04x}-{response_index:012x}",
                        envelope.message_id,
                        decision="REJECTED",
                    ),
                    origin=holder["value"].server_origin,
                )

            sender.on_send = reject
            result = await port.submit(request(MinecraftIntent.QUERY, local_id))
            assert result.admission is MinecraftPortAdmission.DECLINED
        assert port.correlation_count == ledger.action_count == 32
        old_local_id = f"cycle-{cycle}-0"
        clock.value += 300
        assert port.correlation_count == 0
        sent_before = len(sender.sent)
        stop = request(
            MinecraftIntent.STOP,
            f"stop-{cycle}",
            basis_observation_id=None,
            target_request_id=old_local_id,
        )
        assert (await port.cancel(stop)).admission is MinecraftPortAdmission.DECLINED
        assert len(sender.sent) == sent_before


@pytest.mark.asyncio
async def test_active_local_id_cannot_be_overwritten_and_close_clears_correlations():
    port, _, sender, holder = make_port()

    async def accept(envelope):
        port.receive_authoritative(
            authoritative("admission", "e0000000-0000-0000-0000-00000000000e", envelope.message_id),
            origin=holder["value"].server_origin,
        )

    sender.on_send = accept
    await port.submit(request(MinecraftIntent.QUERY, "same-local"))
    with pytest.raises(ValueError, match="already active"):
        await port.submit(request(MinecraftIntent.QUERY, "same-local"))
    assert port.correlation_count == 1
    await port.close()
    assert port.correlation_count == 0


@pytest.mark.asyncio
async def test_monotonic_rollback_after_send_fails_closed_without_extending_deadline():
    clock = MonoClock()
    sender = Sender()
    port, ledger, _, _ = make_port(sender=sender, timeout=5, monotonic=clock)

    async def roll_back(_):
        clock.value = 9.0

    sender.on_send = roll_back
    result = await asyncio.wait_for(port.submit(request(MinecraftIntent.QUERY, "rollback")), timeout=0.2)
    wire_id = sender.sent[0].message_id
    record = ledger._records[wire_id]
    assert result.admission is MinecraftPortAdmission.UNKNOWN
    assert record.phase.value == "UNCERTAIN" and record.quarantined
    assert ledger.effect_fence_quarantined
    assert sender.revoked and sender.revoked[0].binding == binding()
    assert port.pending_count == port.owned_send_task_count == 0
    with pytest.raises(RuntimeError, match="closed|previously failed"):
        await port.submit(request(MinecraftIntent.QUERY, "after-rollback"))


@pytest.mark.asyncio
async def test_monotonic_rollback_while_send_blocked_cancels_owned_task_and_fences():
    clock = MonoClock()
    sender = Sender()

    async def block(_):
        clock.value = 9.0
        await sender.release.wait()

    sender.on_send = block
    port, ledger, _, _ = make_port(sender=sender, timeout=5, monotonic=clock)
    result = await asyncio.wait_for(port.submit(request(MinecraftIntent.QUERY, "rollback-blocked")), timeout=0.2)
    record = ledger._records[sender.sent[0].message_id]
    assert result.admission is MinecraftPortAdmission.UNKNOWN
    assert record.phase.value == "UNCERTAIN" and ledger.effect_fence_quarantined
    assert sender.revoked
    assert port.pending_count == port.owned_send_task_count == 0


@pytest.mark.asyncio
async def test_context_replacement_plus_clock_rollback_revokes_original_request_binding_not_current():
    clock = MonoClock()
    sender = Sender()
    original = context()
    replacement = context(selected_binding=binding(session="b0000000-0000-0000-0000-00000000000b", epoch=2))
    port, ledger, _, holder = make_port(sender=sender, active=original, timeout=5, monotonic=clock)

    async def replace_and_roll_back(_):
        holder["value"] = replacement
        clock.value = 9.0

    sender.on_send = replace_and_roll_back
    result = await asyncio.wait_for(port.submit(request(MinecraftIntent.QUERY, "replace-rollback")), timeout=0.2)
    record = ledger._records[sender.sent[0].message_id]
    assert result.admission is MinecraftPortAdmission.UNKNOWN
    assert record.phase.value == "UNCERTAIN" and record.binding == original.binding
    assert sender.revoked and sender.revoked[0].binding == original.binding
    assert sender.revoked[0].binding != replacement.binding


@pytest.mark.asyncio
async def test_stop_context_replacement_plus_rollback_revokes_original_cancel_binding():
    clock = MonoClock()
    sender = Sender()
    original = context()
    replacement = context(selected_binding=binding(session="c0000000-0000-0000-0000-00000000000c", epoch=2))
    port, _, _, holder = make_port(sender=sender, active=original, timeout=5, monotonic=clock)

    async def accept_target(envelope):
        port.receive_authoritative(
            authoritative("admission", "d0000000-0000-0000-0000-00000000000d", envelope.message_id),
            origin=holder["value"].server_origin,
        )

    sender.on_send = accept_target
    await port.submit(request(MinecraftIntent.QUERY, "target-before-replace"))

    async def replace_and_roll_back_cancel(_):
        holder["value"] = replacement
        clock.value = 9.0

    sender.on_send = replace_and_roll_back_cancel
    result = await asyncio.wait_for(
        port.cancel(
            request(
                MinecraftIntent.STOP,
                "stop-replace-rollback",
                basis_observation_id=None,
                target_request_id="target-before-replace",
            )
        ),
        timeout=0.2,
    )
    assert result.admission is MinecraftPortAdmission.UNKNOWN
    assert sender.revoked and sender.revoked[-1].binding == original.binding
    assert sender.revoked[-1].binding != replacement.binding
