"""Minecraft ingress integration with WorldStage and settlement routing."""

from datetime import datetime, timezone

import pytest
from support.world_stage_fakes import ContextFactory, handling_report

from src.adapter.minecraft import (
    MinecraftFreshness,
    MinecraftObservationAdapter,
    MinecraftPublicSnapshot,
    MinecraftScope,
)
from src.stage import WorldStage
from src.world.world_settlements import WorldSettlementRouter


class NoPlanAgent:
    async def handle_stimulus(self, request, sink, *, context=None):
        return handling_report(request)

    async def realize_action_plan(self, plan, context, sink):
        raise AssertionError("no-plan observation must not execute a plan")


class Subscriber:
    def __init__(self):
        self.handled = []

    def on_fact_handled(self, outcome):
        self.handled.append(outcome)

    def on_fact_plan_executed(self, outcome):
        raise AssertionError("no-plan observation must not execute a plan")


def make_snapshot(event_id="event-1", revision=1):
    return MinecraftPublicSnapshot(
        event_id=event_id,
        occurred_at=datetime(2026, 10, 20, tzinfo=timezone.utc),
        claimed_world_id="default",
        claimed_character_id="luotianyi",
        claimed_session_id="mc-session",
        claimed_epoch=0,
        world_revision=revision,
        action="player_joined",
        action_subject="public-player",
        action_target=None,
        action_detail=None,
        freshness=MinecraftFreshness.FRESH,
    )


@pytest.mark.asyncio
async def test_accepted_ingress_settles_no_plan_outcome_without_claiming_durable_delivery():
    router = WorldSettlementRouter()
    stage = await WorldStage.create(
        character_id="luotianyi",
        world_id="default",
        agent=NoPlanAgent(),
        context_factory=ContextFactory(),
        on_handling_settled=router.on_handling_settled,
    )
    subscriber = Subscriber()
    adapter = MinecraftObservationAdapter(
        scope=MinecraftScope(
            actor_id="relay",
            world_id="default",
            character_id="luotianyi",
            session_id="mc-session",
            epoch=0,
        ),
        target=stage,
        settlements=router,
    )
    try:
        assert await adapter.submit(make_snapshot(), subscriber) is True
        await stage.wait_idle()
    finally:
        await stage.close()

    assert len(subscriber.handled) == 1
    assert subscriber.handled[0].ignored is True
    assert router.awaiting_settlement == ()


@pytest.mark.asyncio
async def test_stage_revision_rejection_cleans_registration_and_duplicate_has_no_cross_owner_discard():
    router = WorldSettlementRouter()
    stage = await WorldStage.create(
        character_id="luotianyi",
        world_id="default",
        agent=NoPlanAgent(),
        context_factory=ContextFactory(),
        on_handling_settled=router.on_handling_settled,
    )
    adapter = MinecraftObservationAdapter(
        scope=MinecraftScope(
            actor_id="relay",
            world_id="default",
            character_id="luotianyi",
            session_id="mc-session",
            epoch=0,
        ),
        target=stage,
        settlements=router,
    )
    first, duplicate = Subscriber(), Subscriber()
    try:
        assert await adapter.submit(make_snapshot(revision=2), first) is True
        assert await adapter.submit(make_snapshot(revision=1), duplicate) is False
        assert len(router.awaiting_settlement) == 1
        await stage.wait_idle()
    finally:
        await stage.close()

    assert len(first.handled) == 1
    assert duplicate.handled == []
