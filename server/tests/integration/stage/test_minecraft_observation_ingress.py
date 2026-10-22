"""Minecraft ingress integration with WorldStage and settlement routing."""

import asyncio
from datetime import datetime, timezone

import pytest
from support.world_stage_fakes import ContextFactory, action_plan, handling_report

from src.adapter.minecraft import (
    MinecraftAdmission,
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


class TwoPlanAgent:
    def __init__(self):
        self.handled = asyncio.Event()
        self.release_execution = asyncio.Event()

    async def handle_stimulus(self, request, sink, *, context=None):
        first, second = action_plan(request, 0), action_plan(request, 1)
        await sink.emit(first)
        await sink.emit(second)
        self.handled.set()
        return handling_report(request, plans=(first.plan_id, second.plan_id))

    async def realize_action_plan(self, plan, context, sink):
        from types import SimpleNamespace

        from src.domain.agent import ExecutionStatus

        await self.release_execution.wait()
        return SimpleNamespace(status=ExecutionStatus.COMPLETED, error_code=None)


class Subscriber:
    def __init__(self):
        self.handled = []

    def on_fact_handled(self, outcome):
        self.handled.append(outcome)

    def on_fact_plan_executed(self, outcome):
        raise AssertionError("no-plan observation must not execute a plan")


class PlanSubscriber(Subscriber):
    def on_fact_plan_executed(self, outcome):
        pass


class DelayedTarget:
    def __init__(self, stage):
        self.character_id = stage.character_id
        self.world_id = stage.world_id
        self._sink = stage.fact_sink
        self.admitted = asyncio.Event()
        self.release = asyncio.Event()

    @property
    def fact_sink(self):
        return self

    async def submit(self, fact):
        accepted = await self._sink.submit(fact)
        self.admitted.set()
        await self.release.wait()
        return accepted


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
        assert await adapter.submit(make_snapshot(), subscriber) is MinecraftAdmission.ACCEPTED
        await stage.wait_idle()
    finally:
        await stage.close()

    assert len(subscriber.handled) == 1
    assert subscriber.handled[0].ignored is True
    assert router.awaiting_settlement == ()


@pytest.mark.asyncio
async def test_terminal_no_plan_settlement_releases_adapter_capacity():
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
        max_in_flight=1,
    )
    try:
        assert await adapter.submit(make_snapshot("one"), Subscriber()) is MinecraftAdmission.ACCEPTED
        await stage.wait_idle()
        assert adapter.owned_observation_count == 0
        assert await adapter.submit(make_snapshot("two", revision=2), Subscriber()) is MinecraftAdmission.ACCEPTED
    finally:
        await adapter.close()
        await stage.close()


@pytest.mark.asyncio
async def test_capacity_remains_owned_until_all_plan_outcomes_settle():
    router = WorldSettlementRouter()
    agent = TwoPlanAgent()
    stage = await WorldStage.create(
        character_id="luotianyi",
        world_id="default",
        agent=agent,
        context_factory=ContextFactory(),
        on_handling_settled=router.on_handling_settled,
        on_execution_finished=router.on_execution_finished,
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
        max_in_flight=1,
    )
    try:
        assert await adapter.submit(make_snapshot(), PlanSubscriber()) is MinecraftAdmission.ACCEPTED
        await agent.handled.wait()
        assert adapter.owned_observation_count == 1
        agent.release_execution.set()
        await stage.wait_idle()
        assert adapter.owned_observation_count == 0
    finally:
        await adapter.close()
        await stage.close()


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
        assert await adapter.submit(make_snapshot(revision=2), first) is MinecraftAdmission.ACCEPTED
        assert await adapter.submit(make_snapshot(revision=1), duplicate) is MinecraftAdmission.REJECTED
        await stage.wait_idle()
    finally:
        await stage.close()

    assert len(first.handled) == 1
    assert duplicate.handled == []


@pytest.mark.asyncio
async def test_cancelled_caller_keeps_subscription_after_sink_admitted_then_settles():
    router = WorldSettlementRouter()
    stage = await WorldStage.create(
        character_id="luotianyi",
        world_id="default",
        agent=NoPlanAgent(),
        context_factory=ContextFactory(),
        on_handling_settled=router.on_handling_settled,
    )
    target = DelayedTarget(stage)
    adapter = MinecraftObservationAdapter(
        scope=MinecraftScope(
            actor_id="relay",
            world_id="default",
            character_id="luotianyi",
            session_id="mc-session",
            epoch=0,
        ),
        target=target,
        settlements=router,
    )
    subscriber = Subscriber()
    task = asyncio.create_task(adapter.submit(make_snapshot(), subscriber))
    try:
        await target.admitted.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        target.release.set()
        await stage.wait_idle()
    finally:
        target.release.set()
        await adapter.close()
        await stage.close()

    assert len(subscriber.handled) == 1
    assert subscriber.handled[0].ignored is True
