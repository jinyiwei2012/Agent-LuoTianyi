"""Consume authoritative Minecraft terminal outcomes without generating plans."""

import src.domain.agent as d
from src.agent.processing.plan_emitter import PlanEmitter


class MinecraftActionOutcomeHandler:
    async def handle(self, request: d.HandleStimulusRequest, plans: PlanEmitter) -> d.HandlingReport:
        if not isinstance(request.stimulus, d.MinecraftActionOutcomeObserved):
            raise TypeError("MinecraftActionOutcomeHandler only handles MinecraftActionOutcomeObserved")
        _ = plans
        pending = tuple(item.stimulus_id for item in request.interaction.pending_stimuli)
        consumed = (request.stimulus.stimulus_id,)
        return d.HandlingReport(
            request_id=request.request_id,
            trigger_stimulus_id=request.stimulus.stimulus_id,
            basis_interaction_revision=request.interaction.interaction_revision,
            request_status=d.HandlingRequestStatus.COMPLETED,
            considered_pending_stimulus_ids=pending,
            consumed_pending_stimulus_ids=consumed,
            retained_pending_stimulus_ids=tuple(item for item in pending if item not in consumed),
            emitted_plan_ids=(),
            error_code=None,
            retryable=False,
        )
