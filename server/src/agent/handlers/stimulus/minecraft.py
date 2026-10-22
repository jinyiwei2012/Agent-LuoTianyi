"""Convert a typed Minecraft observation into at most one action plan."""

from dataclasses import replace

import src.domain.agent as d
from src.agent.processing.plan_emitter import ActionPlanDraft, PlanEmitter
from src.agent.skills.cognitive.minecraft_decision import MinecraftDecisionSkill
from src.agent.skills.invocation import handling_invocation


class MinecraftObservedHandler:
    def __init__(self, decision: MinecraftDecisionSkill, *, world_id: str) -> None:
        self._decision = decision
        self._world_id = world_id

    async def handle(self, request: d.HandleStimulusRequest, plans: PlanEmitter) -> d.HandlingReport:
        observation = request.stimulus
        if not isinstance(observation, d.MinecraftObserved):
            raise TypeError("MinecraftObservedHandler only handles MinecraftObserved")
        decision = await self._decision.decide(handling_invocation(request, plans.context), observation)
        emitted = ()
        if decision.action is not None:
            action = replace(decision.action, world_id=self._world_id)
            receipt = await plans.emit(
                ActionPlanDraft(source_stimulus_ids=(observation.stimulus_id,), actions=(action,))
            )
            emitted = (receipt.plan_id,)
        pending = tuple(item.stimulus_id for item in request.interaction.pending_stimuli)
        consumed = (observation.stimulus_id,)
        return d.HandlingReport(
            request_id=request.request_id,
            trigger_stimulus_id=observation.stimulus_id,
            basis_interaction_revision=request.interaction.interaction_revision,
            request_status=d.HandlingRequestStatus.COMPLETED,
            considered_pending_stimulus_ids=pending,
            consumed_pending_stimulus_ids=consumed,
            retained_pending_stimulus_ids=tuple(item for item in pending if item not in consumed),
            emitted_plan_ids=emitted,
            error_code=None,
            retryable=False,
        )
