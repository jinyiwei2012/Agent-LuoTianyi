import asyncio
from datetime import datetime, timezone

import pytest
from support.world_stage_fakes import ContextFactory

import src.domain.agent as d
from src.adapter.minecraft import MinecraftObservationBinding, TypedMinecraftObservationIngress
from src.adapter.minecraft.wire import MinecraftEnvelope
from src.agent import Agent
from src.agent.handlers.action.minecraft import MinecraftActionHandler
from src.agent.handlers.action.router import ActionRouter
from src.agent.handlers.stimulus.minecraft import MinecraftObservedHandler
from src.agent.handlers.stimulus.router import StimulusRouter
from src.agent.skills.cognitive.minecraft_decision import MinecraftDecisionSkill
from src.agent.skills.contracts import CharacterNarrative
from src.domain.agent.minecraft import (
    MinecraftBoundScope,
    MinecraftDispatchResult,
    MinecraftPortAdmission,
)
from src.stage import WorldStage


class Model:
    async def generate_response(self, **kwargs):
        return '{"decision":"PROPOSE","intent":"PUBLIC_SAY","message":"你好，世界"}'


class BlockingFirstModel(Model):
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def generate_response(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            await self.release.wait()
        return await super().generate_response(**kwargs)


class Port:
    scope = MinecraftBoundScope(world_id="world", body_id="body", session_epoch=4)

    def __init__(self):
        self.requests = []

    async def submit(self, request):
        self.requests.append(request)
        return MinecraftDispatchResult(request_id=request.request_id, admission=MinecraftPortAdmission.ACCEPTED)

    async def cancel(self, request):
        raise AssertionError("model cannot generate STOP")


NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)


def envelope(revision, boot="boot-a", message_id=None, session_id="session", session_epoch=4):
    return MinecraftEnvelope(
        protocol="luosandbox.minecraft.v1",
        type="observation",
        actor_id="server",
        world_id="world",
        body_id="body",
        session_id=session_id,
        session_epoch=session_epoch,
        message_id=message_id or f"obs-{boot}-{revision}",
        timestamp=NOW,
        payload={
            "stamp": {
                "serverBootId": boot,
                "serverRevision": revision,
                "serverTick": revision,
                "eventId": f"event-{boot}-{revision}",
                "evidence": (),
            },
            "audience": "PUBLIC",
            "source": "SERVER_AUTHORITATIVE",
            "freshness": "CURRENT",
            "state": {
                "dimensionId": "minecraft:overworld",
                "position": {"x": 0.0, "y": 64.0, "z": 0.0},
                "yaw": 0.0,
                "pitch": 0.0,
            },
            "capabilities": {
                "QUERY": "AVAILABLE",
                "PUBLIC_SAY": "AVAILABLE",
                "LOOK": "AVAILABLE",
                "BOUNDED_WALK": "AVAILABLE",
                "STOP": "AVAILABLE",
            },
            "untrustedText": (),
        },
    )


@pytest.mark.asyncio
async def test_real_agent_world_stage_decision_dispatch_loop_and_boot_scoped_revision():
    port = Port()
    skill = MinecraftDecisionSkill(Model(), {"luotianyi": CharacterNarrative("洛天依", "友善", "自然")})
    agent = Agent(
        character_id="luotianyi",
        stimulus_router=StimulusRouter(
            [(d.StimulusKind.MINECRAFT_OBSERVED, MinecraftObservedHandler(skill, world_id="world"))]
        ),
        action_router=ActionRouter([(d.ActionKind.MINECRAFT_ACTION, MinecraftActionHandler(port))]),
    )
    stage = await WorldStage.create(
        character_id="luotianyi",
        world_id="world",
        agent=agent,
        context_factory=ContextFactory(),
    )
    binding = MinecraftObservationBinding(
        actor_id="server",
        world_id="world",
        body_id="body",
        session_id="session",
        session_epoch=4,
        server_boot_id="boot-a",
        character_id="luotianyi",
    )
    stage.bind_minecraft_observations(binding.observation_scope)
    ingress = TypedMinecraftObservationIngress(
        binding=binding,
        target=stage,
        now=lambda: NOW,
    )
    try:
        assert await ingress.submit(envelope(9)) is True
        assert await ingress.submit(envelope(8)) is False
        assert await ingress.submit(envelope(9, message_id="different-message")) is False
        assert await ingress.submit(envelope(1, boot="boot-b")) is False
        await stage.wait_idle()
    finally:
        await stage.close()
    assert len(port.requests) == 1
    assert port.requests[0].basis_observation_id == "obs-boot-a-9"
    assert port.requests[0].scope == port.scope
    assert port.requests[0].public_message == "你好，世界"


@pytest.mark.asyncio
async def test_explicit_trusted_rebind_accepts_new_boot_and_rejects_old_delayed_fact():
    port = Port()
    model = BlockingFirstModel()
    skill = MinecraftDecisionSkill(model, {"luotianyi": CharacterNarrative("洛天依", "友善", "自然")})
    agent = Agent(
        character_id="luotianyi",
        stimulus_router=StimulusRouter(
            [(d.StimulusKind.MINECRAFT_OBSERVED, MinecraftObservedHandler(skill, world_id="world"))]
        ),
        action_router=ActionRouter([(d.ActionKind.MINECRAFT_ACTION, MinecraftActionHandler(port))]),
    )
    stage = await WorldStage.create(
        character_id="luotianyi", world_id="world", agent=agent, context_factory=ContextFactory()
    )
    old = MinecraftObservationBinding(
        actor_id="server",
        world_id="world",
        body_id="body",
        session_id="session-a",
        session_epoch=4,
        server_boot_id="boot-a",
        character_id="luotianyi",
    )
    new = MinecraftObservationBinding(
        actor_id="server",
        world_id="world",
        body_id="body",
        session_id="session-b",
        session_epoch=5,
        server_boot_id="boot-b",
        character_id="luotianyi",
    )
    stage.bind_minecraft_observations(old.observation_scope)
    old_ingress = TypedMinecraftObservationIngress(binding=old, target=stage, now=lambda: NOW)
    assert await old_ingress.submit(envelope(9, session_id="session-a")) is True
    await model.entered.wait()
    stage.bind_minecraft_observations(new.observation_scope)
    assert await old_ingress.submit(envelope(10, session_id="session-a")) is False
    model.release.set()
    new_ingress = TypedMinecraftObservationIngress(binding=new, target=stage, now=lambda: NOW)
    new_envelope = envelope(1, boot="boot-b", session_id="session-b", session_epoch=5)
    try:
        assert await new_ingress.submit(new_envelope) is True
        await stage.wait_idle()
    finally:
        await stage.close()
    assert [request.basis_observation_id for request in port.requests] == ["obs-boot-b-1"]


@pytest.mark.asyncio
async def test_untrusted_boot_variants_do_not_expand_stage_revision_state():
    port = Port()
    skill = MinecraftDecisionSkill(None, {"luotianyi": CharacterNarrative("洛天依", "友善", "自然")})
    agent = Agent(
        character_id="luotianyi",
        stimulus_router=StimulusRouter(
            [(d.StimulusKind.MINECRAFT_OBSERVED, MinecraftObservedHandler(skill, world_id="world"))]
        ),
        action_router=ActionRouter([(d.ActionKind.MINECRAFT_ACTION, MinecraftActionHandler(port))]),
    )
    stage = await WorldStage.create(
        character_id="luotianyi", world_id="world", agent=agent, context_factory=ContextFactory()
    )
    binding = MinecraftObservationBinding(
        actor_id="server",
        world_id="world",
        body_id="body",
        session_id="session",
        session_epoch=4,
        server_boot_id="boot-a",
        character_id="luotianyi",
    )
    stage.bind_minecraft_observations(binding.observation_scope)
    ingress = TypedMinecraftObservationIngress(binding=binding, target=stage, now=lambda: NOW)
    try:
        for index in range(50):
            assert await ingress.submit(envelope(1, boot=f"untrusted-{index}")) is False
        assert stage.minecraft_observation_scope == binding.observation_scope
        assert stage._minecraft_revision is None
    finally:
        await stage.close()


@pytest.mark.asyncio
async def test_session_only_rebind_cancels_old_cognition_and_dispatches_only_new_session():
    port = Port()
    model = BlockingFirstModel()
    skill = MinecraftDecisionSkill(model, {"luotianyi": CharacterNarrative("洛天依", "友善", "自然")})
    agent = Agent(
        character_id="luotianyi",
        stimulus_router=StimulusRouter(
            [(d.StimulusKind.MINECRAFT_OBSERVED, MinecraftObservedHandler(skill, world_id="world"))]
        ),
        action_router=ActionRouter([(d.ActionKind.MINECRAFT_ACTION, MinecraftActionHandler(port))]),
    )
    stage = await WorldStage.create(
        character_id="luotianyi", world_id="world", agent=agent, context_factory=ContextFactory()
    )
    old = MinecraftObservationBinding(
        actor_id="server",
        world_id="world",
        body_id="body",
        session_id="session-a",
        session_epoch=4,
        server_boot_id="boot-a",
        character_id="luotianyi",
    )
    new = MinecraftObservationBinding(
        actor_id="server",
        world_id="world",
        body_id="body",
        session_id="session-b",
        session_epoch=4,
        server_boot_id="boot-a",
        character_id="luotianyi",
    )
    stage.bind_minecraft_observations(old.observation_scope)
    old_ingress = TypedMinecraftObservationIngress(binding=old, target=stage, now=lambda: NOW)
    assert await old_ingress.submit(envelope(1, session_id="session-a")) is True
    await model.entered.wait()
    stage.bind_minecraft_observations(new.observation_scope)
    assert stage.minecraft_observation_scope == new.observation_scope
    assert await old_ingress.submit(envelope(2, session_id="session-a")) is False
    delayed = old_ingress.normalize(envelope(3, session_id="session-a"))
    assert delayed is not None
    assert await stage.fact_sink.submit(delayed) is False
    model.release.set()
    new_ingress = TypedMinecraftObservationIngress(binding=new, target=stage, now=lambda: NOW)
    try:
        assert await new_ingress.submit(envelope(1, session_id="session-b")) is True
        await stage.wait_idle()
    finally:
        await stage.close()
    assert [request.basis_observation_id for request in port.requests] == ["obs-boot-a-1"]
