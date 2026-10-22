import asyncio
import json
from datetime import datetime, timezone

import pytest

import src.domain.agent as d
from src.adapter.minecraft.action_ledger import MinecraftActionLedger, TrustedServerOrigin
from src.adapter.minecraft.control_auth import AuthenticatedHandshake, ControlAuthContext
from src.adapter.minecraft.control_binding import AuthenticatedRoleConnection, BindingTuple
from src.adapter.minecraft.terminal_ingress import MinecraftTerminalIngress, TerminalProjectionAdmission
from src.adapter.minecraft.wire import decode_message

NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)
BODY = "10000000-0000-0000-0000-000000000001"
SESSION = "20000000-0000-0000-0000-000000000002"
BOOT = "30000000-0000-0000-0000-000000000003"
REQUEST = "40000000-0000-0000-0000-000000000004"
ADMISSION = "50000000-0000-0000-0000-000000000005"
RECEIPT = "60000000-0000-0000-0000-000000000006"


class Sink:
    def __init__(self):
        self.facts = []

    async def submit(self, fact):
        self.facts.append(fact)
        return True


class ControlledSink(Sink):
    def __init__(self, results=None):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.results = list(results or [True])
        self.error = None

    async def submit(self, fact):
        self.facts.append(fact)
        self.entered.set()
        await self.release.wait()
        if self.error is not None:
            raise self.error
        return self.results.pop(0)


def binding(session=SESSION):
    return BindingTuple(
        actor_id="actor",
        world_id="world",
        body_id=BODY,
        session_id=session,
        session_epoch=1,
        server_boot_id=BOOT,
        game_connection_id="game",
    )


def origin():
    return TrustedServerOrigin(
        connection=AuthenticatedRoleConnection(
            connection_id="server",
            handshake=AuthenticatedHandshake(
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
            ),
        ),
        expected_install_id="server-install",
    )


def envelope(kind, message_id, payload, session=SESSION):
    return decode_message(
        json.dumps(
            {
                "protocol": "luosandbox.minecraft.v1",
                "type": kind,
                "actorId": "actor",
                "worldId": "world",
                "bodyId": BODY,
                "sessionId": session,
                "sessionEpoch": 1,
                "messageId": message_id,
                "timestamp": "2026-10-23T00:00:00.000Z",
                "payload": payload,
            },
            separators=(",", ":"),
        ).encode()
    )


def stamp(revision):
    return {
        "serverBootId": BOOT,
        "serverRevision": revision,
        "serverTick": revision,
        "eventId": f"80000000-0000-0000-0000-{revision:012x}",
        "evidence": [] if revision < 3 else ["90000000-0000-0000-0000-000000000009"],
    }


def request():
    return envelope(
        "request",
        REQUEST,
        {
            "deadline": "2026-10-23T00:00:05.000Z",
            "basis": None,
            "intent": {"kind": "QUERY", "queryKind": "READ_ONLY_SUMMARY"},
        },
    )


def admission():
    return envelope(
        "admission",
        ADMISSION,
        {"requestId": REQUEST, "decision": "ACCEPTED", "reason": None, "stamp": stamp(2)},
    )


def receipt(status="SUCCEEDED"):
    return envelope(
        "receipt",
        RECEIPT,
        {
            "requestId": REQUEST,
            "status": status,
            "reason": None if status == "SUCCEEDED" else "OUTCOME_UNPROVEN",
            "stamp": stamp(3),
            "outcome": {
                "kind": "SUMMARY" if status == "SUCCEEDED" else "UNDETERMINED",
                "effect": "COMPLETE" if status == "SUCCEEDED" else "UNKNOWN",
                "state": (
                    {
                        "dimensionId": "minecraft:overworld",
                        "position": {"x": 1, "y": 64, "z": 2},
                        "yaw": 0,
                        "pitch": 0,
                    }
                    if status == "SUCCEEDED"
                    else None
                ),
                "observationId": "a0000000-0000-0000-0000-00000000000a" if status == "SUCCEEDED" else None,
            },
        },
    )


def graph(sink=None):
    trusted_origin, selected = origin(), binding()
    ledger = MinecraftActionLedger(
        binding=selected,
        server_origin=trusted_origin,
        monotonic_now=lambda: 10.0,
        utc_now=lambda: NOW,
    )
    ingress = MinecraftTerminalIngress(ledger=ledger, binding=selected, character_id="luotianyi", sink=sink)
    ledger.reserve_request(request(), local_action_id="local-action")
    return ledger, ingress, trusted_origin


@pytest.mark.asyncio
async def test_accepted_alone_is_not_world_success_and_receipt_emits_typed_fact_once():
    sink = Sink()
    ledger, ingress, trusted_origin = graph(sink)
    ledger.ingest(admission(), origin=trusted_origin)
    assert sink.facts == []
    terminal = receipt()
    ledger.ingest(terminal, origin=trusted_origin)
    assert await ingress.emit(terminal) is TerminalProjectionAdmission.ACCEPTED
    assert await ingress.emit(terminal) is TerminalProjectionAdmission.ACCEPTED
    assert len(sink.facts) == 1
    fact = sink.facts[0]
    assert isinstance(fact, d.MinecraftActionOutcomeObserved)
    assert fact.local_action_id == "local-action"
    assert fact.outcome.status is d.MinecraftTerminalStatus.SUCCEEDED
    assert fact.outcome.kind is d.MinecraftOutcomeKind.SUMMARY


@pytest.mark.asyncio
async def test_receipt_before_admission_waits_for_join_and_unknown_remains_non_success():
    sink = Sink()
    ledger, ingress, trusted_origin = graph(sink)
    terminal = receipt(status="UNKNOWN")
    ledger.ingest(terminal, origin=trusted_origin)
    assert await ingress.emit(terminal) is TerminalProjectionAdmission.DECLINED
    ledger.ingest(admission(), origin=trusted_origin)
    assert await ingress.emit(terminal) is TerminalProjectionAdmission.ACCEPTED
    fact = sink.facts[0]
    assert fact.outcome.status is d.MinecraftTerminalStatus.UNKNOWN
    assert fact.outcome.effect is d.MinecraftEffect.UNKNOWN
    assert ledger.effect_fence_quarantined


@pytest.mark.asyncio
async def test_concurrent_duplicate_owns_one_sink_submission():
    sink = ControlledSink()
    ledger, ingress, trusted_origin = graph(sink)
    ledger.ingest(admission(), origin=trusted_origin)
    terminal = receipt()
    ledger.ingest(terminal, origin=trusted_origin)
    first = asyncio.create_task(ingress.emit(terminal))
    await sink.entered.wait()
    second = asyncio.create_task(ingress.emit(terminal))
    sink.release.set()
    assert await first is TerminalProjectionAdmission.ACCEPTED
    assert await second is TerminalProjectionAdmission.ACCEPTED
    assert len(sink.facts) == 1 and ingress.owned_count == 1


@pytest.mark.asyncio
async def test_caller_cancel_after_sink_acceptance_keeps_owned_submission_and_replay_does_not_duplicate():
    sink = ControlledSink()
    ledger, ingress, trusted_origin = graph(sink)
    ledger.ingest(admission(), origin=trusted_origin)
    terminal = receipt()
    ledger.ingest(terminal, origin=trusted_origin)
    caller = asyncio.create_task(ingress.emit(terminal))
    await sink.entered.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    sink.release.set()
    await asyncio.sleep(0)
    assert await ingress.emit(terminal) is TerminalProjectionAdmission.ACCEPTED
    assert len(sink.facts) == 1


@pytest.mark.asyncio
async def test_sink_exception_is_uncertain_and_duplicate_does_not_retry():
    sink = ControlledSink()
    sink.error = RuntimeError("boom")
    ledger, ingress, trusted_origin = graph(sink)
    ledger.ingest(admission(), origin=trusted_origin)
    terminal = receipt()
    ledger.ingest(terminal, origin=trusted_origin)
    task = asyncio.create_task(ingress.emit(terminal))
    await sink.entered.wait()
    sink.release.set()
    assert await task is TerminalProjectionAdmission.UNKNOWN
    assert await ingress.emit(terminal) is TerminalProjectionAdmission.UNKNOWN
    assert len(sink.facts) == 1


@pytest.mark.asyncio
async def test_explicit_false_releases_reservation_and_allows_safe_retry():
    sink = ControlledSink(results=[False, True])
    ledger, ingress, trusted_origin = graph(sink)
    ledger.ingest(admission(), origin=trusted_origin)
    terminal = receipt()
    ledger.ingest(terminal, origin=trusted_origin)
    first = asyncio.create_task(ingress.emit(terminal))
    await sink.entered.wait()
    sink.release.set()
    assert await first is TerminalProjectionAdmission.DECLINED
    sink.entered.clear()
    second = asyncio.create_task(ingress.emit(terminal))
    await sink.entered.wait()
    assert await second is TerminalProjectionAdmission.ACCEPTED
    assert len(sink.facts) == 2


@pytest.mark.asyncio
async def test_close_marks_unresolved_projection_uncertain_without_fake_acceptance_or_task_leak():
    sink = ControlledSink()
    ledger, ingress, trusted_origin = graph(sink)
    ledger.ingest(admission(), origin=trusted_origin)
    terminal = receipt()
    ledger.ingest(terminal, origin=trusted_origin)
    caller = asyncio.create_task(ingress.emit(terminal))
    await sink.entered.wait()
    await ingress.close()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert ingress.owned_count == 1
    assert await ingress.emit(terminal) is TerminalProjectionAdmission.DECLINED
