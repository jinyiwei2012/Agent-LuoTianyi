import json
from datetime import datetime, timezone

import pytest
from support.world_stage_fakes import ContextFactory

import src.domain.agent as d
from src.adapter.minecraft.action_ledger import MinecraftActionLedger, TrustedServerOrigin
from src.adapter.minecraft.action_port import ActiveActionContext, OfflineMinecraftActionPort, TrustedActionObservation
from src.adapter.minecraft.control_auth import AuthenticatedHandshake, ControlAuthContext
from src.adapter.minecraft.control_binding import AuthenticatedRoleConnection, BindingTuple
from src.adapter.minecraft.wire import decode_message, encode_message
from src.agent import Agent
from src.agent.handlers.action.minecraft import MinecraftActionHandler
from src.agent.handlers.action.router import ActionRouter
from src.agent.handlers.stimulus.minecraft import MinecraftObservedHandler
from src.agent.handlers.stimulus.router import StimulusRouter
from src.agent.skills.cognitive.minecraft_decision import MinecraftDecisionSkill
from src.agent.skills.contracts import CharacterNarrative
from src.stage import WorldStage

NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)
BODY = "10000000-0000-0000-0000-000000000001"
SESSION = "20000000-0000-0000-0000-000000000002"
BOOT = "30000000-0000-0000-0000-000000000003"
GAME = "40000000-0000-0000-0000-000000000004"
OBSERVATION = "50000000-0000-0000-0000-000000000005"


class Model:
    async def generate_response(self, **kwargs):
        return '{"decision":"PROPOSE","intent":"PUBLIC_SAY","message":"你好，世界"}'


class OutputSink:
    async def emit(self, output):
        raise AssertionError


def origin():
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
        challenge_id="60000000-0000-0000-0000-000000000006",
        ready_proof="proof",
    )
    return TrustedServerOrigin(
        connection=AuthenticatedRoleConnection(connection_id="server-connection", handshake=auth),
        expected_install_id="server-install",
    )


@pytest.mark.asyncio
async def test_real_agent_stage_router_canonical_request_admission_and_separate_receipt():
    binding = BindingTuple(
        actor_id="actor",
        world_id="world",
        body_id=BODY,
        session_id=SESSION,
        session_epoch=1,
        server_boot_id=BOOT,
        game_connection_id=GAME,
    )
    trusted_origin = origin()
    context = ActiveActionContext(
        binding=binding,
        server_origin=trusted_origin,
        observation=TrustedActionObservation(
            observation_id=OBSERVATION,
            server_boot_id=BOOT,
            server_revision=7,
            observed_at=NOW,
            capabilities=d.MinecraftCapabilities(
                query=d.MinecraftCapabilityAvailability.AVAILABLE,
                public_say=d.MinecraftCapabilityAvailability.AVAILABLE,
                look=d.MinecraftCapabilityAvailability.AVAILABLE,
                bounded_walk=d.MinecraftCapabilityAvailability.AVAILABLE,
                stop=d.MinecraftCapabilityAvailability.AVAILABLE,
            ),
        ),
    )
    ledger = MinecraftActionLedger(
        binding=binding,
        server_origin=trusted_origin,
        monotonic_now=lambda: 10.0,
        utc_now=lambda: NOW,
    )

    class Sender:
        async def send(self, envelope):
            decoded = decode_message(encode_message(envelope))
            assert decoded.type == "request"
            admission = {
                "protocol": decoded.protocol,
                "type": "admission",
                "actorId": decoded.actor_id,
                "worldId": decoded.world_id,
                "bodyId": decoded.body_id,
                "sessionId": decoded.session_id,
                "sessionEpoch": decoded.session_epoch,
                "messageId": "70000000-0000-0000-0000-000000000007",
                "timestamp": "2026-10-23T00:00:00.000Z",
                "payload": {
                    "requestId": decoded.message_id,
                    "decision": "ACCEPTED",
                    "reason": None,
                    "stamp": {
                        "serverBootId": BOOT,
                        "serverRevision": 8,
                        "serverTick": 8,
                        "eventId": "80000000-0000-0000-0000-000000000008",
                        "evidence": [],
                    },
                },
            }
            port.receive_authoritative(
                decode_message(json.dumps(admission, separators=(",", ":")).encode()),
                origin=trusted_origin,
            )

        async def revoke(self, requirement):
            raise AssertionError

    ids = iter(("90000000-0000-0000-0000-000000000009",))
    port = OfflineMinecraftActionPort(
        ledger=ledger,
        sender=Sender(),
        active_context=lambda: context,
        monotonic_now=lambda: 10.0,
        utc_now=lambda: NOW,
        message_id=lambda: next(ids),
    )
    skill = MinecraftDecisionSkill(Model(), {"luotianyi": CharacterNarrative("洛天依", "友善", "自然")})
    agent = Agent(
        character_id="luotianyi",
        stimulus_router=StimulusRouter(
            [(d.StimulusKind.MINECRAFT_OBSERVED, MinecraftObservedHandler(skill, world_id="world"))]
        ),
        action_router=ActionRouter([(d.ActionKind.MINECRAFT_ACTION, MinecraftActionHandler(port))]),
    )
    reports = []
    stage = await WorldStage.create(
        character_id="luotianyi",
        world_id="world",
        agent=agent,
        context_factory=ContextFactory(),
        on_execution_finished=lambda plan, report: reports.append((plan, report)),
    )
    stage.bind_minecraft_observations(
        d.MinecraftObservationScope(body_id=BODY, session_id=SESSION, session_epoch=1, server_boot_id=BOOT)
    )
    fact = d.MinecraftObserved(
        stimulus_id=OBSERVATION,
        schema_version=1,
        occurred_at=NOW,
        source=d.StimulusSource.WORLD,
        target_character_ids=("luotianyi",),
        user_id=None,
        ephemeral=True,
        observation_id=OBSERVATION,
        body_id=BODY,
        session_id=SESSION,
        session_epoch=1,
        server_boot_id=BOOT,
        server_revision=7,
        server_tick=7,
        state=d.MinecraftBodyState(
            dimension_id="minecraft:overworld",
            position=d.MinecraftPosition(x=0.0, y=64.0, z=0.0),
            yaw=0.0,
            pitch=0.0,
        ),
        capabilities=context.observation.capabilities,
        evidence_refs=(),
        untrusted_text=(),
    )
    try:
        assert await stage.submit_minecraft_observation(fact, fact_scope(fact))
        await stage.wait_idle()
    finally:
        await stage.close()
    assert reports[0][1].status is d.ExecutionStatus.COMPLETED
    record = ledger.record("90000000-0000-0000-0000-000000000009")
    assert record.admission is not None and record.receipt is None
    receipt = {
        "protocol": "luosandbox.minecraft.v1",
        "type": "receipt",
        "actorId": "actor",
        "worldId": "world",
        "bodyId": BODY,
        "sessionId": SESSION,
        "sessionEpoch": 1,
        "messageId": "a0000000-0000-0000-0000-00000000000a",
        "timestamp": "2026-10-23T00:00:00.000Z",
        "payload": {
            "requestId": record.request_id,
            "status": "SUCCEEDED",
            "reason": None,
            "stamp": {
                "serverBootId": BOOT,
                "serverRevision": 9,
                "serverTick": 9,
                "eventId": "b0000000-0000-0000-0000-00000000000b",
                "evidence": ["c0000000-0000-0000-0000-00000000000c"],
            },
            "outcome": {"kind": "PUBLIC_BROADCAST", "effect": "COMPLETE", "state": None, "observationId": None},
        },
    }
    port.receive_authoritative(
        decode_message(json.dumps(receipt, separators=(",", ":")).encode()),
        origin=trusted_origin,
    )
    assert ledger.record(record.request_id).receipt.payload["status"] == "SUCCEEDED"


def fact_scope(fact):
    return d.MinecraftObservationScope(
        body_id=fact.body_id,
        session_id=fact.session_id,
        session_epoch=fact.session_epoch,
        server_boot_id=fact.server_boot_id,
    )
