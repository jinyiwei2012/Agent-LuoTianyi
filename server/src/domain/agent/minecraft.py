"""Pure Minecraft proposal and runtime-pre-bound dispatch contracts."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

_DIMENSION = re.compile(r"^[a-z0-9_.-]+:[a-z0-9/._-]+$")


def is_minecraft_dimension(value: object) -> bool:
    return isinstance(value, str) and _DIMENSION.fullmatch(value) is not None


def is_minecraft_coordinate(value: object) -> bool:
    return type(value) is float and math.isfinite(value) and abs(value) <= 30000000


def is_minecraft_public_message(value: object) -> bool:
    if not isinstance(value, str) or not value or "\0" in value or "\r" in value or "\n" in value:
        return False
    try:
        return not value.lstrip().startswith("/") and len(value) <= 256 and len(value.encode("utf-8")) <= 1024
    except UnicodeEncodeError:
        return False


class MinecraftIntent(str, Enum):
    QUERY = "QUERY"
    PUBLIC_SAY = "PUBLIC_SAY"
    LOOK = "LOOK"
    BOUNDED_WALK = "BOUNDED_WALK"
    STOP = "STOP"


class MinecraftCapabilityAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True, kw_only=True)
class MinecraftCapabilities:
    query: MinecraftCapabilityAvailability
    public_say: MinecraftCapabilityAvailability
    look: MinecraftCapabilityAvailability
    bounded_walk: MinecraftCapabilityAvailability
    stop: MinecraftCapabilityAvailability

    def __post_init__(self):
        if any(
            not isinstance(value, MinecraftCapabilityAvailability)
            for value in (self.query, self.public_say, self.look, self.bounded_walk, self.stop)
        ):
            raise ValueError("invalid Minecraft capabilities")

    def availability(self, intent: MinecraftIntent) -> MinecraftCapabilityAvailability:
        return {
            MinecraftIntent.QUERY: self.query,
            MinecraftIntent.PUBLIC_SAY: self.public_say,
            MinecraftIntent.LOOK: self.look,
            MinecraftIntent.BOUNDED_WALK: self.bounded_walk,
            MinecraftIntent.STOP: self.stop,
        }[intent]


@dataclass(frozen=True, slots=True, kw_only=True)
class MinecraftUntrustedText:
    event_id: str
    text: str

    def __post_init__(self):
        if (
            not isinstance(self.event_id, str)
            or not self.event_id.strip()
            or not isinstance(self.text, str)
            or not self.text
            or "\0" in self.text
            or len(self.text) > 1024
            or len(self.text.encode("utf-8")) > 4096
        ):
            raise ValueError("invalid Minecraft untrusted text")


class MinecraftPortAdmission(str, Enum):
    ACCEPTED = "accepted"
    DECLINED = "declined"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True, kw_only=True)
class MinecraftPosition:
    x: float
    y: float
    z: float

    def __post_init__(self):
        if any(not is_minecraft_coordinate(v) for v in (self.x, self.y, self.z)):
            raise ValueError("invalid Minecraft position")


@dataclass(frozen=True, slots=True, kw_only=True)
class MinecraftBodyState:
    dimension_id: str
    position: MinecraftPosition
    yaw: float
    pitch: float

    def __post_init__(self):
        if not is_minecraft_dimension(self.dimension_id) or not isinstance(self.position, MinecraftPosition):
            raise ValueError("invalid Minecraft state")
        if (
            type(self.yaw) not in (int, float)
            or type(self.pitch) not in (int, float)
            or not math.isfinite(self.yaw)
            or not math.isfinite(self.pitch)
            or not -180 <= self.yaw < 180
            or not -90 <= self.pitch <= 90
        ):
            raise ValueError("invalid Minecraft orientation")


@dataclass(frozen=True, slots=True, kw_only=True)
class MinecraftBoundScope:
    world_id: str
    body_id: str
    session_epoch: int

    def __post_init__(self):
        if (
            any(not isinstance(v, str) or not v.strip() for v in (self.world_id, self.body_id))
            or type(self.session_epoch) is not int
            or self.session_epoch < 0
        ):
            raise ValueError("invalid bound scope")


@dataclass(frozen=True, slots=True, kw_only=True)
class MinecraftObservationScope:
    """Trusted Stage owner fence for one authenticated server boot."""

    body_id: str
    session_id: str
    session_epoch: int
    server_boot_id: str

    def __post_init__(self):
        if (
            any(
                not isinstance(value, str) or not value.strip()
                for value in (self.body_id, self.session_id, self.server_boot_id)
            )
            or type(self.session_epoch) is not int
            or self.session_epoch < 0
        ):
            raise ValueError("invalid Minecraft observation scope")


@dataclass(frozen=True, slots=True, kw_only=True)
class MinecraftActionRequest:
    request_id: str
    scope: MinecraftBoundScope
    intent: MinecraftIntent
    basis_observation_id: str | None
    public_message: str | None = None
    target: MinecraftBodyState | None = None
    maximum_distance: float | None = None
    target_request_id: str | None = None

    def __post_init__(self):
        if (
            not isinstance(self.request_id, str)
            or not self.request_id.strip()
            or not isinstance(self.scope, MinecraftBoundScope)
            or not isinstance(self.intent, MinecraftIntent)
        ):
            raise ValueError("invalid Minecraft request")
        effect = self.intent in {MinecraftIntent.PUBLIC_SAY, MinecraftIntent.LOOK, MinecraftIntent.BOUNDED_WALK}
        if (
            (self.intent is not MinecraftIntent.QUERY and self.basis_observation_id is not None) != effect
            or (
                self.basis_observation_id is not None
                and (not isinstance(self.basis_observation_id, str) or not self.basis_observation_id.strip())
            )
            or (self.public_message is not None) != (self.intent is MinecraftIntent.PUBLIC_SAY)
            or (self.target is not None) != (self.intent in {MinecraftIntent.LOOK, MinecraftIntent.BOUNDED_WALK})
            or (self.maximum_distance is not None) != (self.intent is MinecraftIntent.BOUNDED_WALK)
            or (self.target_request_id is not None) != (self.intent is MinecraftIntent.STOP)
        ):
            raise ValueError("invalid Minecraft intent fields")
        if self.public_message is not None and not is_minecraft_public_message(self.public_message):
            raise ValueError("invalid public message")
        if self.maximum_distance is not None and (
            type(self.maximum_distance) not in (int, float)
            or not math.isfinite(self.maximum_distance)
            or not 0 < self.maximum_distance <= 4
        ):
            raise ValueError("invalid Minecraft distance")
        if self.target_request_id is not None and (
            not isinstance(self.target_request_id, str) or not self.target_request_id.strip()
        ):
            raise ValueError("invalid stop target")


@dataclass(frozen=True, slots=True, kw_only=True)
class MinecraftDispatchResult:
    request_id: str
    admission: MinecraftPortAdmission

    def __post_init__(self):
        if (
            not isinstance(self.request_id, str)
            or not self.request_id.strip()
            or not isinstance(self.admission, MinecraftPortAdmission)
        ):
            raise ValueError("invalid dispatch result")


class MinecraftActionPort(Protocol):
    @property
    def scope(self) -> MinecraftBoundScope: ...
    async def submit(self, request: MinecraftActionRequest) -> MinecraftDispatchResult: ...
    async def cancel(self, request: MinecraftActionRequest) -> MinecraftDispatchResult: ...
