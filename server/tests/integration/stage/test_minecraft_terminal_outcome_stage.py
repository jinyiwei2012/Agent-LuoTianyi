from datetime import datetime, timezone

import pytest
from support.world_stage_fakes import ContextFactory

import src.domain.agent as d
from src.agent import Agent
from src.agent.handlers.action.router import ActionRouter
from src.agent.handlers.stimulus.minecraft_outcome import MinecraftActionOutcomeHandler
from src.agent.handlers.stimulus.router import StimulusRouter
from src.stage import WorldStage

NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)


def outcome():
    return d.MinecraftActionOutcomeObserved(
        stimulus_id="minecraft-outcome:receipt",
        schema_version=1,
        occurred_at=NOW,
        source=d.StimulusSource.WORLD,
        target_character_ids=("luotianyi",),
        user_id=None,
        ephemeral=True,
        receipt_id="receipt",
        request_id="request",
        local_action_id="action",
        body_id="body",
        session_id="session",
        session_epoch=1,
        server_boot_id="boot",
        server_revision=3,
        server_tick=3,
        evidence_refs=(),
        outcome=d.MinecraftTerminalOutcome(
            status=d.MinecraftTerminalStatus.UNKNOWN,
            reason="OUTCOME_UNPROVEN",
            kind=d.MinecraftOutcomeKind.UNDETERMINED,
            effect=d.MinecraftEffect.UNKNOWN,
            state=None,
            observation_id=None,
        ),
    )


@pytest.mark.asyncio
async def test_real_stage_agent_outcome_handler_consumes_without_plan_or_memory_dependency():
    plans = []
    agent = Agent(
        character_id="luotianyi",
        stimulus_router=StimulusRouter(
            [(d.StimulusKind.MINECRAFT_ACTION_OUTCOME_OBSERVED, MinecraftActionOutcomeHandler())]
        ),
        action_router=ActionRouter(()),
    )
    stage = await WorldStage.create(
        character_id="luotianyi",
        world_id="world",
        agent=agent,
        context_factory=ContextFactory(),
        on_execution_finished=lambda plan, report: plans.append(plan),
    )
    scope = d.MinecraftObservationScope(body_id="body", session_id="session", session_epoch=1, server_boot_id="boot")
    stage.bind_minecraft_observations(scope)
    try:
        assert await stage.submit_minecraft_outcome(outcome(), scope)
        await stage.wait_idle()
    finally:
        await stage.close()
    assert plans == []
