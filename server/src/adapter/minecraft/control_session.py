"""Default-disabled offline composition owner for one Minecraft control session."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

import src.domain.agent as d
from src.adapter.minecraft.action_ledger import MinecraftActionLedger, TrustedServerOrigin
from src.adapter.minecraft.action_port import (
    ActiveActionContext,
    CanonicalActionSender,
    OfflineMinecraftActionPort,
    TrustedActionObservation,
)
from src.adapter.minecraft.control_binding import (
    ActiveBindingSnapshot,
    MinecraftControlBindingCoordinator,
)
from src.adapter.minecraft.terminal_ingress import MinecraftTerminalIngress, TerminalOutcomeSink

_MAX_RETIRED = 2
_RETIRED_SECONDS = 300.0


class ControlSessionError(RuntimeError):
    """Fail-closed composition error; it grants no transport or game authority."""


@dataclass(slots=True)
class _OwnedGeneration:
    snapshot: ActiveBindingSnapshot
    context: ActiveActionContext
    ledger: MinecraftActionLedger
    port: OfflineMinecraftActionPort
    activated_at: float
    retired_at: float | None = None
    current: bool = True
    closing: bool = False
    terminal_ingress: MinecraftTerminalIngress | None = None


class RuntimeMinecraftActionPort:
    """Stable facade that forwards only to the composition's current generation."""

    def __init__(self, owner: "MinecraftControlSession") -> None:
        self._owner = owner

    @property
    def scope(self) -> d.MinecraftBoundScope:
        return self._owner._require_port().scope

    async def submit(self, request: d.MinecraftActionRequest) -> d.MinecraftDispatchResult:
        return await self._owner._require_port().submit(request)

    async def cancel(self, request: d.MinecraftActionRequest) -> d.MinecraftDispatchResult:
        return await self._owner._require_port().cancel(request)


class MinecraftControlSession:
    """Compose coordinator ACTIVE state, ledger, port and trusted observation.

    The owner is disabled by default.  It creates a fresh ledger/port for every
    coordinator generation and never turns a retired generation into a current
    grant.  Retired ledgers remain bounded historical correlation only.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        coordinator: MinecraftControlBindingCoordinator,
        sender: CanonicalActionSender,
        monotonic_now: Callable[[], float],
        utc_now: Callable[[], datetime],
        admission_timeout_seconds: float = 5.0,
        character_id: str = "luotianyi",
        terminal_sink: TerminalOutcomeSink | None = None,
    ) -> None:
        if type(enabled) is not bool:
            raise TypeError("Minecraft control session enabled must be bool")
        if not callable(monotonic_now) or not callable(utc_now):
            raise TypeError("Minecraft control session clocks must be callable")
        self._enabled = enabled
        self._coordinator = coordinator
        self._sender = sender
        self._monotonic_now = monotonic_now
        self._utc_now = utc_now
        self._admission_timeout_seconds = admission_timeout_seconds
        self._character_id = character_id
        self._terminal_sink = terminal_sink
        self._current: _OwnedGeneration | None = None
        self._retired: list[_OwnedGeneration] = []
        self._closed = False
        self.runtime_port = RuntimeMinecraftActionPort(self)

    @property
    def active_context(self) -> ActiveActionContext | None:
        generation = self._current
        if not self._generation_is_current(generation):
            return None
        return generation.context

    @property
    def retired_count(self) -> int:
        self._prune_retired(self._monotonic())
        return len(self._retired)

    async def activate(self, observation: d.MinecraftObserved) -> RuntimeMinecraftActionPort:
        """Publish one coherent generation from one synchronous coordinator snapshot."""
        if not self._enabled or self._closed:
            raise ControlSessionError("Minecraft control session is disabled")
        snapshot = self._coordinator.active_snapshot()
        if snapshot is None:
            raise ControlSessionError("Minecraft coordinator is not ACTIVE")
        trusted_observation = self._trusted_observation(snapshot, observation)
        existing = self._current
        if existing is not None and existing.snapshot == snapshot:
            existing.context = ActiveActionContext(
                binding=snapshot.active.binding,
                server_origin=existing.context.server_origin,
                observation=trusted_observation,
            )
            return self.runtime_port
        now = self._monotonic()
        self._prune_retired(now)
        if self._current is not None and len(self._retired) >= _MAX_RETIRED:
            raise ControlSessionError("retired Minecraft history capacity")
        await self._retire_current(now)
        origin = TrustedServerOrigin(
            connection=snapshot.server,
            expected_install_id=snapshot.server.install_id,
        )
        context = ActiveActionContext(
            binding=snapshot.active.binding,
            server_origin=origin,
            observation=trusted_observation,
        )
        ledger = MinecraftActionLedger(
            binding=snapshot.active.binding,
            server_origin=origin,
            monotonic_now=self._monotonic_now,
            utc_now=self._utc_now,
        )
        generation = _OwnedGeneration(
            snapshot=snapshot,
            context=context,
            ledger=ledger,
            port=None,
            activated_at=now,
        )
        generation.port = OfflineMinecraftActionPort(
            ledger=ledger,
            sender=self._sender,
            active_context=lambda: self._context_for(generation),
            monotonic_now=self._monotonic_now,
            utc_now=self._utc_now,
            admission_timeout_seconds=self._admission_timeout_seconds,
        )
        generation.terminal_ingress = MinecraftTerminalIngress(
            ledger=ledger,
            binding=snapshot.active.binding,
            character_id=self._character_id,
            sink=self._terminal_sink,
        )
        self._current = generation
        self._prune_retired(now)
        return self.runtime_port

    async def synchronize(self) -> None:
        """Retire a generation whose coordinator authority was synchronously fenced."""
        if self._current is not None and not self._generation_is_current(self._current):
            await self._retire_current(self._monotonic())

    async def receive_authoritative(self, envelope, *, origin: TrustedServerOrigin) -> None:
        current = self._current
        if self._generation_is_current(current) and envelope.session_id == current.context.binding.session_id:
            current.port.receive_authoritative(envelope, origin=origin)
            if envelope.type == "receipt":
                await current.terminal_ingress.emit(envelope)
            return
        for retired in self._retired:
            if envelope.session_id == retired.context.binding.session_id:
                retired.ledger.ingest(envelope, origin=origin)
                return
        raise ControlSessionError("authoritative message has no owned generation")

    def history_outcome(self, envelope, *, origin: TrustedServerOrigin) -> d.MinecraftActionOutcomeObserved:
        """Project an exact retired-session receipt for audit without current cognition."""
        for retired in self._retired:
            if envelope.session_id != retired.context.binding.session_id:
                continue
            retired.ledger.ingest(envelope, origin=origin)
            if envelope.type != "receipt":
                raise ControlSessionError("historical outcome requires a receipt")
            return retired.terminal_ingress.fact(envelope)
        raise ControlSessionError("historical receipt has no retired generation")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._retire_current(self._monotonic())

    def _require_port(self) -> OfflineMinecraftActionPort:
        current = self._current
        if not self._generation_is_current(current):
            raise ControlSessionError("Minecraft action port has no current generation")
        return current.port

    def _context_for(self, generation: _OwnedGeneration) -> ActiveActionContext | None:
        return generation.context if self._generation_is_current(generation) else None

    def _generation_is_current(self, generation: _OwnedGeneration | None) -> bool:
        return bool(
            self._enabled
            and not self._closed
            and generation is not None
            and generation.current
            and self._current is generation
            and self._coordinator.active_snapshot() == generation.snapshot
        )

    async def _retire_current(self, now: float) -> None:
        generation, self._current = self._current, None
        if generation is None:
            return
        generation.current = False
        if generation not in self._retired:
            generation.retired_at = now
            self._retired.append(generation)
        self._prune_retired(now)
        if generation.closing:
            return
        generation.closing = True
        try:
            await generation.terminal_ingress.close()
            await generation.port.close()
        except asyncio.CancelledError:
            raise
        finally:
            generation.closing = False

    def _prune_retired(self, now: float) -> None:
        self._retired = [
            item for item in self._retired if item.retired_at is None or now - item.retired_at < _RETIRED_SECONDS
        ]

    @staticmethod
    def _trusted_observation(
        snapshot: ActiveBindingSnapshot,
        observation: d.MinecraftObserved,
    ) -> TrustedActionObservation:
        binding = snapshot.active.binding
        if (
            not isinstance(observation, d.MinecraftObserved)
            or observation.body_id != binding.body_id
            or observation.session_id != binding.session_id
            or observation.session_epoch != binding.session_epoch
            or observation.server_boot_id != binding.server_boot_id
        ):
            raise ControlSessionError("trusted observation does not match ACTIVE binding")
        return TrustedActionObservation(
            observation_id=observation.observation_id,
            server_boot_id=observation.server_boot_id,
            server_revision=observation.server_revision,
            observed_at=observation.occurred_at,
            capabilities=observation.capabilities,
        )

    def _monotonic(self) -> float:
        value = self._monotonic_now()
        if type(value) not in (int, float) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
            raise ControlSessionError("invalid control session monotonic clock")
        return float(value)
