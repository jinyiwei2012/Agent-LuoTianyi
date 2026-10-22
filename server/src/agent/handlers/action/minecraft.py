"""Typed Minecraft proposal dispatch; completion means dispatch only, not world success."""

from __future__ import annotations

import src.domain.agent as d
from src.agent.processing.output_emitter import OutputEmitter
from src.domain.agent.minecraft import (
    MinecraftActionPort,
    MinecraftActionRequest,
    MinecraftBodyState,
    MinecraftIntent,
    MinecraftPortAdmission,
    MinecraftPosition,
)


class MinecraftActionHandler:
    def __init__(self, port: MinecraftActionPort) -> None:
        self._port = port

    async def realize(
        self, action: d.Action, execution_context: d.ExecutionContext, outputs: OutputEmitter
    ) -> d.ActionResult:
        _ = execution_context, outputs
        if not isinstance(action, d.MinecraftAction):
            raise TypeError("MinecraftActionHandler only handles MinecraftAction")
        target = (
            None
            if action.target_dimension_id is None
            else MinecraftBodyState(
                dimension_id=action.target_dimension_id,
                position=MinecraftPosition(x=action.target_x, y=action.target_y, z=action.target_z),
                yaw=0,
                pitch=0,
            )
        )
        request = MinecraftActionRequest(
            request_id=action.action_id,
            scope=self._port.scope,
            intent=MinecraftIntent(action.intent.value),
            basis_observation_id=action.basis_observation_id,
            public_message=action.public_message,
            target=target,
            maximum_distance=action.maximum_distance,
            target_request_id=action.target_request_id,
        )
        result = await (
            self._port.cancel(request) if action.intent is d.MinecraftActionIntent.STOP else self._port.submit(request)
        )
        reference = d.EffectRef(kind=d.EffectKind.MINECRAFT_ACTION_REQUEST, effect_id=action.action_id)
        if result.request_id != action.action_id or result.admission is MinecraftPortAdmission.UNKNOWN:
            return d.ActionResult(
                action_id=action.action_id,
                status=d.ActionExecutionStatus.FAILED,
                error_code=d.ExecutionErrorCode.MINECRAFT_ADMISSION_UNKNOWN,
                irreversible_effect_committed=True,
                effect_ref=reference,
            )
        if result.admission is MinecraftPortAdmission.ACCEPTED:
            return d.ActionResult(
                action_id=action.action_id,
                status=d.ActionExecutionStatus.COMPLETED,
                error_code=None,
                irreversible_effect_committed=True,
                effect_ref=reference,
            )
        return d.ActionResult(
            action_id=action.action_id,
            status=d.ActionExecutionStatus.FAILED,
            error_code=d.ExecutionErrorCode.DEPENDENCY_UNAVAILABLE,
            irreversible_effect_committed=False,
            effect_ref=None,
        )
