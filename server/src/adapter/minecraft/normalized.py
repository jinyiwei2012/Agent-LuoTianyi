"""Adapter compatibility surface for typed Minecraft domain values.

Authorization is supplied by a runtime-bound port, never by decoded payloads.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.domain.agent.minecraft import MinecraftBodyState


@dataclass(frozen=True, slots=True, kw_only=True)
class MinecraftCapabilities:
    query: str
    public_say: str
    look: str
    bounded_walk: str
    stop: str

    def __post_init__(self) -> None:
        if any(
            value not in {"AVAILABLE", "UNAVAILABLE", "UNKNOWN"}
            for value in (self.query, self.public_say, self.look, self.bounded_walk, self.stop)
        ):
            raise ValueError("invalid Minecraft capability")


@dataclass(frozen=True, slots=True, kw_only=True)
class MinecraftObservation:
    observation_id: str
    world_id: str
    body_id: str
    state: MinecraftBodyState
    capabilities: MinecraftCapabilities
    freshness: str
    server_revision: int

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (self.observation_id, self.world_id, self.body_id)
        ):
            raise ValueError("invalid Minecraft identity")
        if not isinstance(self.state, MinecraftBodyState) or not isinstance(self.capabilities, MinecraftCapabilities):
            raise ValueError("invalid Minecraft observation")
        if (
            self.freshness not in {"CURRENT", "STALE", "UNKNOWN"}
            or type(self.server_revision) is not int
            or self.server_revision < 0
        ):
            raise ValueError("invalid Minecraft observation metadata")
