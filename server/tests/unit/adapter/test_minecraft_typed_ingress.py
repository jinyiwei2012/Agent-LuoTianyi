import json
from datetime import datetime, timedelta, timezone

import pytest

import src.domain.agent as d
from src.adapter.minecraft import MinecraftObservationBinding, TypedMinecraftObservationIngress
from src.adapter.minecraft.wire import decode_message


class Sink:
    def __init__(self):
        self.facts = []

    async def submit(self, fact):
        self.facts.append(fact)
        return True


class Target:
    character_id = "luotianyi"
    world_id = "world"

    def __init__(self, scope):
        self.fact_sink = Sink()
        self.minecraft_observation_scope = scope

    async def submit_minecraft_observation(self, fact, scope):
        if scope != self.minecraft_observation_scope:
            return False
        return await self.fact_sink.submit(fact)


NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)


def envelope(*, freshness="CURRENT", timestamp="2026-10-23T00:00:00.000Z", boot_id=None, **changes):
    value = {
        "protocol": "luosandbox.minecraft.v1",
        "type": "observation",
        "actorId": "server",
        "worldId": "world",
        "bodyId": "10000000-0000-0000-0000-000000000001",
        "sessionId": "20000000-0000-0000-0000-000000000002",
        "sessionEpoch": 3,
        "messageId": "30000000-0000-0000-0000-000000000003",
        "timestamp": timestamp,
        "payload": {
            "stamp": {
                "serverBootId": boot_id or "40000000-0000-0000-0000-000000000004",
                "serverRevision": 1,
                "serverTick": 2,
                "eventId": "50000000-0000-0000-0000-000000000005",
                "evidence": ["60000000-0000-0000-0000-000000000006"],
            },
            "audience": "PUBLIC",
            "source": "SERVER_AUTHORITATIVE",
            "freshness": freshness,
            "state": {
                "dimensionId": "minecraft:overworld",
                "position": {"x": 1, "y": 64, "z": 2},
                "yaw": 0,
                "pitch": 0,
            },
            "capabilities": {key: "AVAILABLE" for key in ("QUERY", "PUBLIC_SAY", "LOOK", "BOUNDED_WALK", "STOP")},
            "untrustedText": [{"eventId": "70000000-0000-0000-0000-000000000007", "text": "hello"}],
        },
    }
    value.update(changes)
    return decode_message(json.dumps(value, separators=(",", ":")).encode())


def binding():
    return MinecraftObservationBinding(
        actor_id="server",
        world_id="world",
        body_id="10000000-0000-0000-0000-000000000001",
        session_id="20000000-0000-0000-0000-000000000002",
        session_epoch=3,
        server_boot_id="40000000-0000-0000-0000-000000000004",
        character_id="luotianyi",
    )


def ingress(*, now=lambda: NOW, installed=None):
    trusted = binding()
    return TypedMinecraftObservationIngress(
        binding=trusted,
        target=Target(trusted.observation_scope if installed is None else installed),
        now=now,
    )


@pytest.mark.asyncio
async def test_normalizes_typed_fact_without_summary_parsing():
    trusted = binding()
    target = Target(trusted.observation_scope)
    adapter = TypedMinecraftObservationIngress(binding=trusted, target=target, now=lambda: NOW)
    assert await adapter.submit(envelope()) is True
    fact = target.fact_sink.facts[0]
    assert isinstance(fact, d.MinecraftObserved)
    assert fact.state.position == d.MinecraftPosition(x=1.0, y=64.0, z=2.0)
    assert fact.server_boot_id.startswith("4000")
    assert fact.untrusted_text[0].text == "hello"


def test_binding_mismatch_and_stale_fail_closed():
    adapter = ingress()
    assert adapter.normalize(envelope(actorId="other")) is None
    assert adapter.normalize(envelope(freshness="STALE")) is None
    assert adapter.normalize(envelope(boot_id="80000000-0000-0000-0000-000000000008")) is None


@pytest.mark.parametrize(
    ("offset", "accepted"),
    [
        (timedelta(hours=-1), False),
        (timedelta(milliseconds=-1000), True),
        (timedelta(milliseconds=-1001), False),
        (timedelta(milliseconds=1000), True),
        (timedelta(milliseconds=1001), False),
    ],
)
def test_trusted_clock_enforces_inclusive_ttl_and_future_skew(offset, accepted):
    timestamp = (NOW + offset).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    assert (ingress().normalize(envelope(timestamp=timestamp)) is not None) is accepted


@pytest.mark.parametrize(
    "now",
    [
        None,
        lambda: datetime(2026, 10, 23),
        lambda: "not-a-clock",
        lambda: (_ for _ in ()).throw(RuntimeError("clock failed")),
    ],
)
def test_missing_or_invalid_clock_cannot_bypass_freshness(now):
    if now is None:
        with pytest.raises(TypeError):
            TypedMinecraftObservationIngress(binding=binding(), target=Target(binding().observation_scope), now=now)
    else:
        assert ingress(now=now).normalize(envelope()) is None


def test_ingress_requires_matching_stage_installed_scope():
    with pytest.raises(ValueError, match="not installed"):
        ingress(
            installed=d.MinecraftObservationScope(
                body_id="other",
                session_id="session",
                session_epoch=3,
                server_boot_id="boot",
            )
        )
