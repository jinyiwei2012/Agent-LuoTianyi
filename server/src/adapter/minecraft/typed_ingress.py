"""Authenticated typed Minecraft observation ingress.

The caller supplies a strict decoded envelope plus an already authenticated
binding. Payload identity claims never select a character or WorldStage.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

import src.domain.agent as d
from src.adapter.minecraft.wire import MinecraftEnvelope
from src.stage import WorldFactSink


@dataclass(frozen=True, slots=True, kw_only=True)
class MinecraftObservationBinding:
    actor_id: str
    world_id: str
    body_id: str
    session_id: str
    session_epoch: int
    server_boot_id: str
    character_id: str

    def __post_init__(self) -> None:
        strings = (
            self.actor_id,
            self.world_id,
            self.body_id,
            self.session_id,
            self.server_boot_id,
            self.character_id,
        )
        if any(not isinstance(value, str) or not value.strip() for value in strings):
            raise ValueError("Minecraft binding strings must be nonblank")
        if type(self.session_epoch) is not int or self.session_epoch < 0:
            raise ValueError("Minecraft binding epoch must be nonnegative")

    @property
    def observation_scope(self) -> d.MinecraftObservationScope:
        return d.MinecraftObservationScope(
            body_id=self.body_id,
            session_id=self.session_id,
            session_epoch=self.session_epoch,
            server_boot_id=self.server_boot_id,
        )


class MinecraftObservationTarget(Protocol):
    character_id: str
    world_id: str
    fact_sink: WorldFactSink
    minecraft_observation_scope: d.MinecraftObservationScope | None

    async def submit_minecraft_observation(
        self,
        fact: d.MinecraftObserved,
        scope: d.MinecraftObservationScope,
    ) -> bool: ...


class TypedMinecraftObservationIngress:
    """Normalize observations for a Stage fence installed by a trusted caller.

    Construction does not authenticate or change the Stage fence. The caller
    must first install the binding through WorldStage.bind_minecraft_observations
    after its external authentication and quiescence checks succeed.
    """

    _OBSERVATION_TTL = timedelta(milliseconds=1000)
    _FUTURE_SKEW = timedelta(milliseconds=1000)

    def __init__(
        self,
        *,
        binding: MinecraftObservationBinding,
        target: MinecraftObservationTarget,
        now: Callable[[], datetime],
    ) -> None:
        if target.character_id != binding.character_id or target.world_id != binding.world_id:
            raise ValueError("Minecraft binding does not match WorldStage")
        if target.minecraft_observation_scope != binding.observation_scope:
            raise ValueError("Minecraft binding is not installed on WorldStage")
        if not callable(now):
            raise TypeError("Minecraft observation clock must be callable")
        self._binding = binding
        self._now = now
        self._target = target

    async def submit(self, envelope: MinecraftEnvelope) -> bool:
        fact = self.normalize(envelope)
        return (
            False
            if fact is None
            else await self._target.submit_minecraft_observation(fact, self._binding.observation_scope)
        )

    def normalize(self, envelope: MinecraftEnvelope) -> d.MinecraftObserved | None:
        binding = self._binding
        if (
            not isinstance(envelope, MinecraftEnvelope)
            or envelope.type != "observation"
            or envelope.actor_id != binding.actor_id
            or envelope.world_id != binding.world_id
            or envelope.body_id != binding.body_id
            or envelope.session_id != binding.session_id
            or envelope.session_epoch != binding.session_epoch
        ):
            return None
        payload = envelope.payload
        if (
            payload["freshness"] != "CURRENT"
            or payload["stamp"]["serverBootId"] != binding.server_boot_id
            or not self._timestamp_is_current(envelope.timestamp)
        ):
            return None
        stamp, state, capabilities = payload["stamp"], payload["state"], payload["capabilities"]
        return d.MinecraftObserved(
            stimulus_id=envelope.message_id,
            schema_version=1,
            occurred_at=envelope.timestamp,
            source=d.StimulusSource.WORLD,
            target_character_ids=(binding.character_id,),
            user_id=None,
            ephemeral=True,
            observation_id=envelope.message_id,
            body_id=binding.body_id,
            session_id=binding.session_id,
            session_epoch=binding.session_epoch,
            server_boot_id=stamp["serverBootId"],
            server_revision=stamp["serverRevision"],
            server_tick=stamp["serverTick"],
            state=d.MinecraftBodyState(
                dimension_id=state["dimensionId"],
                position=d.MinecraftPosition(**state["position"]),
                yaw=state["yaw"],
                pitch=state["pitch"],
            ),
            capabilities=d.MinecraftCapabilities(
                query=d.MinecraftCapabilityAvailability(capabilities["QUERY"]),
                public_say=d.MinecraftCapabilityAvailability(capabilities["PUBLIC_SAY"]),
                look=d.MinecraftCapabilityAvailability(capabilities["LOOK"]),
                bounded_walk=d.MinecraftCapabilityAvailability(capabilities["BOUNDED_WALK"]),
                stop=d.MinecraftCapabilityAvailability(capabilities["STOP"]),
            ),
            evidence_refs=tuple(d.EvidenceRef(evidence_id=value) for value in stamp["evidence"]),
            untrusted_text=tuple(
                d.MinecraftUntrustedText(event_id=item["eventId"], text=item["text"])
                for item in payload["untrustedText"]
            ),
        )

    def _timestamp_is_current(self, timestamp: datetime) -> bool:
        try:
            now = self._now()
            if (
                not isinstance(now, datetime)
                or now.tzinfo is None
                or now.utcoffset() != timedelta(0)
                or timestamp.tzinfo is None
                or timestamp.utcoffset() != timedelta(0)
            ):
                return False
            now = now.astimezone(timezone.utc)
            timestamp = timestamp.astimezone(timezone.utc)
            return now - timestamp <= self._OBSERVATION_TTL and timestamp - now <= self._FUTURE_SKEW
        except Exception:
            return False
