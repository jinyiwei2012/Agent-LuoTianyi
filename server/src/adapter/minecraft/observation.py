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
from src.world.world_settlements import FactHandlingOutcome, FactPlanOutcome, FactSettlementSubscriber


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


class MinecraftAdmission(str, Enum):
    """Ingress result; UNKNOWN retains settlement ownership until the stage settles."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
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

    ACCEPTED means the stage accepted the observation for handling. It is not a
    durable-delivery or exactly-once acknowledgement. UNKNOWN means admission
    could not be observed; its settlement registration is deliberately retained.
    """

    _OBSERVATION_KIND = "minecraft_public_snapshot"

    def __init__(
        self,
        *,
        scope: MinecraftScope,
        target: MinecraftWorldTarget,
        settlements: MinecraftSettlementPort,
        max_in_flight: int = 8,
    ) -> None:
        if target.character_id != scope.character_id or target.world_id != scope.world_id:
            raise ValueError("Minecraft scope does not match the actual WorldStage target")
        if type(max_in_flight) is not int or max_in_flight < 1:
            raise ValueError("max_in_flight must be a positive integer")
        self._scope = scope
        self._target = target
        self._settlements = settlements
        # This caps all owned registrations, including UNKNOWN outcomes. WorldStage
        # admission is normally synchronous, so eight is conservative.
        self._max_in_flight = max_in_flight
        self._in_flight: dict[asyncio.Task[bool], str] = {}
        self._owned: dict[str, _OwnedSubscriber] = {}
        self._abandoned_unknown = 0
        self._closed = False

    async def submit(
        self, snapshot: MinecraftPublicSnapshot, subscriber: FactSettlementSubscriber
    ) -> MinecraftAdmission:
        """Submit once; caller cancellation shields admission and preserves uncertain ownership."""
        if not isinstance(snapshot, MinecraftPublicSnapshot) or not self._is_permitted(snapshot):
            return MinecraftAdmission.REJECTED
        if self._closed or len(self._owned) >= self._max_in_flight:
            return MinecraftAdmission.REJECTED
        fact = self._build_fact(snapshot)
        owned = _OwnedSubscriber(self, fact.stimulus_id, subscriber)
        try:
            self._settlements.register(fact.stimulus_id, owned)
        except Exception:
            return MinecraftAdmission.REJECTED
        self._owned[fact.stimulus_id] = owned
        task = asyncio.create_task(self._target.fact_sink.submit(fact), name="minecraft-observation-admission")
        self._in_flight[task] = fact.stimulus_id
        task.add_done_callback(self._admission_done)
        try:
            accepted = await asyncio.shield(task)
        except asyncio.CancelledError:
            # The task continues and its callback decides known rejection versus
            # unknown/accepted ownership. Never drop a possible settlement.
            raise
        except Exception:
            return MinecraftAdmission.UNKNOWN
        if accepted is True:
            return MinecraftAdmission.ACCEPTED
        if accepted is False:
            self._discard_owned(fact.stimulus_id)
            return MinecraftAdmission.REJECTED
        return MinecraftAdmission.UNKNOWN

    async def close(self) -> None:
        """Stop new admission and cancel/drain submits without deleting unsettled registrations.

        The owning WorldStage must settle/drain before explicit abandonment. This
        method is not permission to discard observations while the stage is active.
        """
        self._closed = True
        tasks = tuple(self._in_flight)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def abandon_after_stage_quiesced(self) -> int:
        """Explicitly abandon retained unknown ownership after caller verified stage quiescence.

        The caller must only use this after the owned WorldStage fully stopped and
        drained. It is an audited count, not an automatic TTL cleanup.
        """
        abandoned = len(self._owned)
        for stimulus_id in tuple(self._owned):
            self._discard_owned(stimulus_id)
        self._abandoned_unknown += abandoned
        return abandoned

    @property
    def owned_observation_count(self) -> int:
        """Number of registered observations still owned by this adapter."""
        return len(self._owned)

    @property
    def abandoned_unknown_count(self) -> int:
        """Cumulative explicit post-quiescence abandonments."""
        return self._abandoned_unknown

    def _admission_done(self, task: asyncio.Task[bool]) -> None:
        stimulus_id = self._in_flight.pop(task, None)
        if stimulus_id is None or task.cancelled():
            return
        try:
            accepted = task.result()
        except Exception:
            return
        if accepted is False:
            self._discard_owned(stimulus_id)
        # True, malformed results, and exceptions retain the registration:
        # only an explicit False proves that this adapter's admission failed.

    def _settled(self, stimulus_id: str) -> None:
        self._owned.pop(stimulus_id, None)

    def _discard_owned(self, stimulus_id: str) -> None:
        if self._owned.pop(stimulus_id, None) is not None:
            self._settlements.discard(stimulus_id)

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


class _OwnedSubscriber:
    """Release adapter capacity exactly when settlement routing reaches its terminal point."""

    def __init__(
        self,
        adapter: MinecraftObservationAdapter,
        stimulus_id: str,
        subscriber: FactSettlementSubscriber,
    ) -> None:
        self._adapter = adapter
        self._stimulus_id = stimulus_id
        self._subscriber = subscriber
        self._pending_plan_ids: set[str] | None = None

    def on_fact_handled(self, outcome: FactHandlingOutcome) -> None:
        try:
            self._subscriber.on_fact_handled(outcome)
        finally:
            if not outcome.plan_ids:
                self._adapter._settled(self._stimulus_id)
            else:
                self._pending_plan_ids = set(outcome.plan_ids)

    def on_fact_plan_executed(self, outcome: FactPlanOutcome) -> None:
        try:
            self._subscriber.on_fact_plan_executed(outcome)
        finally:
            if self._pending_plan_ids is not None:
                self._pending_plan_ids.discard(outcome.plan.plan_id)
                if not self._pending_plan_ids:
                    self._adapter._settled(self._stimulus_id)
