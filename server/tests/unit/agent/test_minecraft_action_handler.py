import pytest

import src.domain.agent as d
from src.agent.handlers.action.minecraft import MinecraftActionHandler
from src.agent.handlers.action.router import ActionRouter
from src.domain.agent.minecraft import MinecraftBoundScope, MinecraftDispatchResult, MinecraftPortAdmission


class Port:
    def __init__(self, admission, response_id="action"):
        self.admission = admission
        self.response_id = response_id
        self.requests = []
        self.scope = MinecraftBoundScope(world_id="trusted-world", body_id="trusted-body", session_epoch=9)

    async def submit(self, request):
        self.requests.append(request)
        return MinecraftDispatchResult(request_id=self.response_id, admission=self.admission)

    async def cancel(self, request):
        self.requests.append(request)
        return MinecraftDispatchResult(request_id=self.response_id, admission=self.admission)


def action():
    return d.MinecraftAction(
        action_id="action",
        request_id="request",
        world_id="world",
        body_id="body",
        session_epoch=0,
        intent=d.MinecraftActionIntent.QUERY,
        basis_observation_id=None,
        public_message=None,
        target_dimension_id=None,
        target_x=None,
        target_y=None,
        target_z=None,
        maximum_distance=None,
        target_request_id=None,
    )


@pytest.mark.asyncio
async def test_registered_handler_reports_dispatch_not_world_success():
    port = Port(MinecraftPortAdmission.ACCEPTED)
    handler = ActionRouter([(d.ActionKind.MINECRAFT_ACTION, MinecraftActionHandler(port))]).resolve(
        d.ActionKind.MINECRAFT_ACTION
    )
    result = await handler.realize(action(), None, None)
    assert result.status is d.ActionExecutionStatus.COMPLETED
    assert result.effect_ref == d.EffectRef(kind=d.EffectKind.MINECRAFT_ACTION_REQUEST, effect_id="action")
    assert port.requests[0].intent.value == d.MinecraftActionIntent.QUERY.value


@pytest.mark.asyncio
async def test_unknown_admission_is_not_retried_or_reported_completed():
    port = Port(MinecraftPortAdmission.UNKNOWN)
    result = await MinecraftActionHandler(port).realize(action(), None, None)
    assert result.status is d.ActionExecutionStatus.FAILED
    assert result.error_code is d.ExecutionErrorCode.MINECRAFT_ADMISSION_UNKNOWN
    assert result.effect_ref is not None
    assert len(port.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "intent,changes",
    [
        (d.MinecraftActionIntent.QUERY, {}),
        (d.MinecraftActionIntent.PUBLIC_SAY, {"basis_observation_id": "obs", "public_message": "hello"}),
        (
            d.MinecraftActionIntent.LOOK,
            {
                "basis_observation_id": "obs",
                "target_dimension_id": "minecraft:overworld",
                "target_x": 1.0,
                "target_y": 65.0,
                "target_z": 2.0,
            },
        ),
        (
            d.MinecraftActionIntent.BOUNDED_WALK,
            {
                "basis_observation_id": "obs",
                "target_dimension_id": "minecraft:overworld",
                "target_x": 1.0,
                "target_y": 65.0,
                "target_z": 2.0,
                "maximum_distance": 4.0,
            },
        ),
        (d.MinecraftActionIntent.STOP, {"target_request_id": "prior"}),
    ],
)
async def test_all_verbs_forward_only_typed_proposal_and_trusted_scope(intent, changes):
    values = dict(
        action_id="action",
        request_id="request",
        world_id="untrusted-world",
        body_id="untrusted-body",
        session_epoch=0,
        intent=intent,
        basis_observation_id=None,
        public_message=None,
        target_dimension_id=None,
        target_x=None,
        target_y=None,
        target_z=None,
        maximum_distance=None,
        target_request_id=None,
    )
    values.update(changes)
    port = Port(MinecraftPortAdmission.ACCEPTED)
    await MinecraftActionHandler(port).realize(d.MinecraftAction(**values), None, None)
    request = port.requests[0]
    assert request.scope == port.scope
    assert request.intent.value == intent.value
    assert (request.target_request_id == "prior") is (intent is d.MinecraftActionIntent.STOP)
    if intent is d.MinecraftActionIntent.BOUNDED_WALK:
        assert request.target.position.x == 1 and request.maximum_distance == 4


@pytest.mark.asyncio
async def test_mismatched_port_response_keeps_uncertain_request_reference():
    port = Port(MinecraftPortAdmission.ACCEPTED, response_id="wrong")
    result = await MinecraftActionHandler(port).realize(action(), None, None)
    assert result.error_code is d.ExecutionErrorCode.MINECRAFT_ADMISSION_UNKNOWN
    assert result.effect_ref.effect_id == "action"


@pytest.mark.asyncio
async def test_query_with_basis_forwards_to_port():
    port = Port(MinecraftPortAdmission.ACCEPTED)
    proposal = action()
    proposal = d.MinecraftAction(
        action_id=proposal.action_id,
        request_id=proposal.request_id,
        world_id=proposal.world_id,
        body_id=proposal.body_id,
        session_epoch=proposal.session_epoch,
        intent=proposal.intent,
        basis_observation_id="obs",
        public_message=None,
        target_dimension_id=None,
        target_x=None,
        target_y=None,
        target_z=None,
        maximum_distance=None,
        target_request_id=None,
    )
    await MinecraftActionHandler(port).realize(proposal, None, None)
    assert port.requests[0].basis_observation_id == "obs"
