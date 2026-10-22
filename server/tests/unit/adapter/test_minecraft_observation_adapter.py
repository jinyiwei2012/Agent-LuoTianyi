"""Unit contracts for bounded Minecraft public-observation ingress."""

import asyncio
from datetime import datetime, timedelta, timezone, tzinfo

import pytest

import src.domain.agent as d
from src.adapter.minecraft import (
    MinecraftAdmission,
    MinecraftAuthority,
    MinecraftFreshness,
    MinecraftObservationAdapter,
    MinecraftPublicSnapshot,
    MinecraftScope,
    MinecraftVisibility,
)


class Sink:
    def __init__(self, result=True, error=None):
        self.result, self.error, self.facts = result, error, []

    async def submit(self, fact):
        self.facts.append(fact)
        if self.error is not None:
            raise self.error
        return self.result


class Target:
    def __init__(self, sink, *, character_id="luotianyi", world_id="world-a"):
        self.fact_sink = sink
        self.character_id = character_id
        self.world_id = world_id


class Settlements:
    def __init__(self):
        self.owners = {}
        self.discarded = []

    def register(self, stimulus_id, subscriber):
        if stimulus_id in self.owners:
            raise ValueError("duplicate")
        self.owners[stimulus_id] = subscriber

    def discard(self, stimulus_id):
        self.discarded.append(stimulus_id)
        self.owners.pop(stimulus_id, None)


class Subscriber:
    def on_fact_handled(self, outcome):
        pass

    def on_fact_plan_executed(self, outcome):
        pass


def scope(**changes):
    values = {
        "actor_id": "relay-7",
        "world_id": "world-a",
        "character_id": "luotianyi",
        "session_id": "session-1",
        "epoch": 2,
    }
    values.update(changes)
    return MinecraftScope(**values)


def snapshot(**changes):
    values = {
        "event_id": "event-1",
        "occurred_at": datetime(2026, 10, 20, tzinfo=timezone.utc),
        "claimed_world_id": "world-a",
        "claimed_character_id": "luotianyi",
        "claimed_session_id": "session-1",
        "claimed_epoch": 2,
        "world_revision": 7,
        "action": "block_broken",
        "action_subject": "stone",
        "action_target": "player",
        "action_detail": "overworld",
        "freshness": MinecraftFreshness.FRESH,
        "evidence_refs": ("minecraft:evidence:1",),
    }
    values.update(changes)
    return MinecraftPublicSnapshot(**values)


@pytest.mark.asyncio
async def test_builds_fixed_public_world_observation_and_registers_before_submit():
    sink, settlements = Sink(), Settlements()
    adapter = MinecraftObservationAdapter(scope=scope(), target=Target(sink), settlements=settlements)

    assert await adapter.submit(snapshot(), Subscriber()) is MinecraftAdmission.ACCEPTED

    fact = sink.facts[0]
    assert isinstance(fact, d.WorldObservation)
    assert fact.source is d.StimulusSource.WORLD
    assert fact.user_id is None
    assert fact.target_character_ids == ("luotianyi",)
    assert fact.schema_version == 1
    assert fact.observation_kind.value == "minecraft_public_snapshot"
    assert fact.world_revision == 7
    assert fact.evidence_refs == (d.EvidenceRef(evidence_id="minecraft:evidence:1"),)
    assert "block_broken" in fact.fact.summary
    assert fact.stimulus_id in settlements.owners


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"claimed_world_id": "world-b"},
        {"claimed_character_id": "other"},
        {"claimed_session_id": "old"},
        {"claimed_epoch": 3},
        {"visibility": MinecraftVisibility.RESTRICTED},
        {"freshness": MinecraftFreshness.STALE},
        {"freshness": MinecraftFreshness.UNKNOWN},
    ],
)
async def test_scope_or_privacy_mismatch_fails_closed_without_registering(change):
    sink, settlements = Sink(), Settlements()
    adapter = MinecraftObservationAdapter(scope=scope(), target=Target(sink), settlements=settlements)

    assert await adapter.submit(snapshot(**change), Subscriber()) is MinecraftAdmission.REJECTED
    assert sink.facts == []
    assert settlements.owners == {}


@pytest.mark.asyncio
async def test_client_local_is_rejected_unless_explicitly_allowlisted():
    local = snapshot(authority=MinecraftAuthority.CLIENT_LOCAL)
    sink, settlements = Sink(), Settlements()
    adapter = MinecraftObservationAdapter(scope=scope(), target=Target(sink), settlements=settlements)
    assert await adapter.submit(local, Subscriber()) is MinecraftAdmission.REJECTED
    assert (
        await MinecraftObservationAdapter(
            scope=scope(allow_client_local=True),
            target=Target(sink),
            settlements=settlements,
        ).submit(local, Subscriber())
        is MinecraftAdmission.ACCEPTED
    )


@pytest.mark.asyncio
async def test_stage_rejection_and_exception_discard_only_own_successful_registration():
    subscriber = Subscriber()
    rejected_sink, rejected_settlements = Sink(result=False), Settlements()
    rejected = MinecraftObservationAdapter(
        scope=scope(), target=Target(rejected_sink), settlements=rejected_settlements
    )
    assert await rejected.submit(snapshot(), subscriber) is MinecraftAdmission.REJECTED
    assert rejected_settlements.owners == {}

    broken_sink, broken_settlements = Sink(error=RuntimeError("boom")), Settlements()
    broken = MinecraftObservationAdapter(scope=scope(), target=Target(broken_sink), settlements=broken_settlements)
    assert await broken.submit(snapshot(), subscriber) is MinecraftAdmission.UNKNOWN
    assert len(broken_settlements.owners) == 1

    duplicate_settlements = Settlements()
    duplicate_sink = Sink()
    duplicate = MinecraftObservationAdapter(
        scope=scope(), target=Target(duplicate_sink), settlements=duplicate_settlements
    )
    owner = Subscriber()
    assert await duplicate.submit(snapshot(), owner) is MinecraftAdmission.ACCEPTED
    assert await duplicate.submit(snapshot(), subscriber) is MinecraftAdmission.REJECTED
    assert len(duplicate_settlements.owners) == 1
    assert duplicate_settlements.discarded == []


@pytest.mark.asyncio
async def test_unknown_admission_capacity_bounds_retained_subscriptions_before_registering():
    settlements = Settlements()
    adapter = MinecraftObservationAdapter(
        scope=scope(), target=Target(Sink(error=RuntimeError("unknown"))), settlements=settlements, max_in_flight=2
    )

    assert await adapter.submit(snapshot(event_id="one"), Subscriber()) is MinecraftAdmission.UNKNOWN
    assert await adapter.submit(snapshot(event_id="two"), Subscriber()) is MinecraftAdmission.UNKNOWN
    assert await adapter.submit(snapshot(event_id="three"), Subscriber()) is MinecraftAdmission.REJECTED
    assert len(settlements.owners) == 2
    assert adapter.owned_observation_count == 2


def test_invalid_timestamps_identifiers_and_revisions_are_rejected():
    with pytest.raises(ValueError):
        snapshot(occurred_at=datetime(2026, 10, 20))
    with pytest.raises(ValueError):
        snapshot(event_id=" ")
    with pytest.raises(ValueError):
        snapshot(world_revision=-1)
    with pytest.raises(ValueError):
        snapshot(world_revision=True)
    with pytest.raises(ValueError):
        scope(epoch=True)


class _NoOffsetTimezone(tzinfo):
    def utcoffset(self, value):
        return None

    def dst(self, value):
        return timedelta(0)


def test_missing_freshness_and_non_offset_timezone_are_rejected():
    values = {
        "event_id": "event",
        "occurred_at": datetime(2026, 10, 20, tzinfo=timezone.utc),
        "claimed_world_id": "world-a",
        "claimed_character_id": "luotianyi",
        "claimed_session_id": "session-1",
        "claimed_epoch": 2,
        "world_revision": 1,
        "action": "action",
        "action_subject": "subject",
        "action_target": None,
        "action_detail": None,
    }
    with pytest.raises(TypeError):
        MinecraftPublicSnapshot(**values)
    with pytest.raises(ValueError):
        snapshot(occurred_at=datetime(2026, 10, 20, tzinfo=_NoOffsetTimezone()))


def test_target_must_be_the_scope_bound_actual_stage():
    with pytest.raises(ValueError):
        MinecraftObservationAdapter(scope=scope(), target=Target(Sink(), world_id="other"), settlements=Settlements())
    with pytest.raises(ValueError):
        MinecraftObservationAdapter(
            scope=scope(), target=Target(Sink(), character_id="other"), settlements=Settlements()
        )


@pytest.mark.asyncio
async def test_caller_cancellation_after_stage_admission_keeps_settlement_owner():
    class DelayedAcceptedSink(Sink):
        def __init__(self):
            super().__init__()
            self.admitted = asyncio.Event()
            self.release = asyncio.Event()

        async def submit(self, fact):
            self.facts.append(fact)
            self.admitted.set()
            await self.release.wait()
            return True

    settlements = Settlements()
    sink = DelayedAcceptedSink()
    adapter = MinecraftObservationAdapter(scope=scope(), target=Target(sink), settlements=settlements)
    submitter = asyncio.create_task(adapter.submit(snapshot(), Subscriber()))
    await sink.admitted.wait()
    submitter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await submitter
    assert len(settlements.owners) == 1
    sink.release.set()
    await asyncio.sleep(0)
    assert len(settlements.owners) == 1
    await adapter.close()


def test_stable_identity_is_unambiguous_when_ids_contain_old_delimiter():
    left = MinecraftObservationAdapter(
        scope=scope(actor_id="a\x1fb", session_id="c"),
        target=Target(Sink()),
        settlements=Settlements(),
    )
    right = MinecraftObservationAdapter(
        scope=scope(actor_id="a", session_id="b\x1fc"),
        target=Target(Sink()),
        settlements=Settlements(),
    )
    assert left._stable_identity("event") != right._stable_identity("event")
