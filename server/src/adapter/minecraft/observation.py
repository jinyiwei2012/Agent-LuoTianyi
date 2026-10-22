"""Translate a bound public Minecraft snapshot into a world fact.

The adapter accepts only context already bound by its caller.  It neither
parses Minecraft messages nor authenticates external identities.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

import src.domain.agent as d
from src.stage import WorldFactSink
from src.world.world_settlements import FactSettlementSubscriber


class MinecraftAuthority(str, Enum):
    """Authority attached to a normalized snapshot."""

    SERVER_AUTHORITATIVE = "server_authoritative"
    CLIENT_LOCAL = "client_local"


class MinecraftVisibility(str, Enum):
    """Visibility classification accepted by this deliberately public seam."""

    PUBLIC = "public"
    RESTRICTED = "restricted"


class MinecraftFreshness(str, Enum):
    """Caller-validated freshness result; TTL verification remains outside this seam."""

    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True, kw_only=True)
class MinecraftScope:
    """Trusted, caller-bound permission scope for one Minecraft ingress path."""

    actor_id: str
    world_id: str
    character_id: str
    session_id: str
    epoch: int
    source: d.StimulusSource = d.StimulusSource.WORLD
    audience: MinecraftVisibility = MinecraftVisibility.PUBLIC
    allow_client_local: bool = False

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value.strip() for value in self._string_fields()):
            raise ValueError("Minecraft scope strings must be nonblank")
        if type(self.epoch) is not int or self.epoch < 0:
            raise ValueError("epoch must be a nonnegative integer")
        if self.source is not d.StimulusSource.WORLD or self.audience is not MinecraftVisibility.PUBLIC:
            raise ValueError("Minecraft scope must be public world ingress")
        if type(self.allow_client_local) is not bool:
            raise ValueError("allow_client_local must be a bool")

    def _string_fields(self) -> tuple[str, str, str, str]:
        return self.actor_id, self.world_id, self.character_id, self.session_id


@dataclass(frozen=True, slots=True, kw_only=True)
class MinecraftPublicSnapshot:
    """Fixed, normalized, public-only Minecraft observation.

    The generic content fields are untrusted public content, never permissions or
    capabilities. They stay structured rather than carrying an unbounded JSON
    body. Evidence references are opaque allowed references, not payload text.
    """

    event_id: str
    occurred_at: datetime
    claimed_world_id: str
    claimed_character_id: str
    claimed_session_id: str
    claimed_epoch: int
    world_revision: int
    action: str
    action_subject: str
    action_target: str | None
    action_detail: str | None
    freshness: MinecraftFreshness
    authority: MinecraftAuthority = MinecraftAuthority.SERVER_AUTHORITATIVE
    visibility: MinecraftVisibility = MinecraftVisibility.PUBLIC
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        required = (
            self.event_id,
            self.claimed_world_id,
            self.claimed_character_id,
            self.claimed_session_id,
            self.action,
            self.action_subject,
        )
        if any(not isinstance(value, str) or not value.strip() for value in required):
            raise ValueError("Minecraft snapshot required strings must be nonblank")
        if self.action_target is not None and (
            not isinstance(self.action_target, str) or not self.action_target.strip()
        ):
            raise ValueError("action_target must be nonblank when supplied")
        if self.action_detail is not None and (
            not isinstance(self.action_detail, str) or not self.action_detail.strip()
        ):
            raise ValueError("action_detail must be nonblank when supplied")
        if (
            not isinstance(self.occurred_at, datetime)
            or self.occurred_at.tzinfo is None
            or self.occurred_at.utcoffset() is None
        ):
            raise ValueError("occurred_at must be timezone-aware")
        if type(self.claimed_epoch) is not int or self.claimed_epoch < 0:
            raise ValueError("claimed_epoch must be a nonnegative integer")
        if type(self.world_revision) is not int or self.world_revision < 0:
            raise ValueError("world_revision must be a nonnegative integer")
        if (
            not isinstance(self.authority, MinecraftAuthority)
            or not isinstance(self.visibility, MinecraftVisibility)
            or not isinstance(self.freshness, MinecraftFreshness)
        ):
            raise ValueError("authority, visibility, and freshness must be Minecraft enums")
        if not isinstance(self.evidence_refs, tuple) or any(
            not isinstance(ref, str) or not ref.strip() for ref in self.evidence_refs
        ):
            raise ValueError("evidence_refs must be a tuple of nonblank references")


class MinecraftSettlementPort(Protocol):
    """The only settlement operations required by this ingress seam."""

    def register(self, stimulus_id: str, subscriber: FactSettlementSubscriber) -> None: ...

    def discard(self, stimulus_id: str) -> None: ...


class MinecraftWorldTarget(Protocol):
    """Bound target exposing only the actual WorldStage identity and fact sink."""

    @property
    def character_id(self) -> str: ...

    @property
    def world_id(self) -> str: ...

    @property
    def fact_sink(self) -> WorldFactSink: ...


class MinecraftObservationAdapter:
    """Fail-closed ingress for fresh public Minecraft observations.

    ``True`` means the stage accepted the observation for handling.  It is not
    a durable-delivery or exactly-once acknowledgement.
    """

    _OBSERVATION_KIND = "minecraft_public_snapshot"

    def __init__(
        self,
        *,
        scope: MinecraftScope,
        target: MinecraftWorldTarget,
        settlements: MinecraftSettlementPort,
    ) -> None:
        if target.character_id != scope.character_id or target.world_id != scope.world_id:
            raise ValueError("Minecraft scope does not match the actual WorldStage target")
        self._scope = scope
        self._target = target
        self._settlements = settlements

    async def submit(self, snapshot: MinecraftPublicSnapshot, subscriber: FactSettlementSubscriber) -> bool:
        """Build, register, and submit one snapshot without retrying uncertain admission."""
        if not isinstance(snapshot, MinecraftPublicSnapshot) or not self._is_permitted(snapshot):
            return False
        fact = self._build_fact(snapshot)
        registered = False
        try:
            self._settlements.register(fact.stimulus_id, subscriber)
            registered = True
            accepted = await self._target.fact_sink.submit(fact)
        except asyncio.CancelledError:
            if registered:
                self._settlements.discard(fact.stimulus_id)
            raise
        except Exception:
            if registered:
                self._settlements.discard(fact.stimulus_id)
            return False
        if not accepted:
            self._settlements.discard(fact.stimulus_id)
        return accepted

    def _is_permitted(self, snapshot: MinecraftPublicSnapshot) -> bool:
        scope = self._scope
        return (
            snapshot.claimed_world_id == scope.world_id
            and snapshot.claimed_character_id == scope.character_id
            and snapshot.claimed_session_id == scope.session_id
            and snapshot.claimed_epoch == scope.epoch
            and snapshot.visibility is MinecraftVisibility.PUBLIC
            and snapshot.freshness is MinecraftFreshness.FRESH
            and (
                snapshot.authority is MinecraftAuthority.SERVER_AUTHORITATIVE
                or (snapshot.authority is MinecraftAuthority.CLIENT_LOCAL and scope.allow_client_local)
            )
        )

    def _build_fact(self, snapshot: MinecraftPublicSnapshot) -> d.WorldObservation:
        identity = self._stable_identity(snapshot.event_id)
        summary = self._summary(snapshot)
        return d.WorldObservation(
            stimulus_id=identity,
            schema_version=1,
            occurred_at=snapshot.occurred_at,
            source=d.StimulusSource.WORLD,
            target_character_ids=(self._scope.character_id,),
            user_id=None,
            ephemeral=False,
            observation_kind=d.WorldObservationKind(value=self._OBSERVATION_KIND),
            fact=d.WorldFact(fact_id=identity, summary=summary),
            evidence_refs=tuple(d.EvidenceRef(evidence_id=ref) for ref in snapshot.evidence_refs),
            world_revision=snapshot.world_revision,
        )

    def _stable_identity(self, event_id: str) -> str:
        material = json.dumps(
            [
                "agentluo:minecraft-public-observation:v1",
                self._scope.actor_id,
                self._scope.world_id,
                self._scope.character_id,
                self._scope.session_id,
                self._scope.epoch,
                event_id,
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return "minecraft:" + hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _summary(snapshot: MinecraftPublicSnapshot) -> str:
        fields = [f"action={snapshot.action}", f"subject={snapshot.action_subject}"]
        if snapshot.action_target is not None:
            fields.append(f"target={snapshot.action_target}")
        if snapshot.action_detail is not None:
            fields.append(f"detail={snapshot.action_detail}")
        return "Minecraft public snapshot: " + "; ".join(fields)
