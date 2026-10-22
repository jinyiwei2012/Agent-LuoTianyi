import pytest

import src.domain.agent as d
from src.agent_runtime import agent_runtime as runtime_module
from src.domain.agent.minecraft import MinecraftBoundScope


class Port:
    scope = MinecraftBoundScope(world_id="world", body_id="body", session_epoch=1)

    async def submit(self, request):
        raise AssertionError

    async def cancel(self, request):
        raise AssertionError


def config(kwargs, **minecraft):
    value = {key: item for key, item in kwargs["config"].items()}
    value["agent"] = {**value["agent"], "minecraft": minecraft}
    return value


def test_runtime_configuration_matrix(runtime_dependencies):
    kwargs, _ = runtime_dependencies
    with pytest.raises(ValueError, match="requires agent.minecraft.enabled"):
        runtime_module.AgentRuntime(**kwargs, minecraft_action_port=Port())
    with pytest.raises(ValueError, match="requires minecraft_action_port"):
        runtime_module.AgentRuntime(**{**kwargs, "config": config(kwargs, enabled=True, character_id="luotianyi")})
    with pytest.raises(TypeError, match="must be a bool"):
        runtime_module.AgentRuntime(**{**kwargs, "config": config(kwargs, enabled="true", character_id="luotianyi")})


@pytest.mark.asyncio
async def test_enabled_runtime_registers_only_configured_character_and_model_can_be_absent(runtime_dependencies):
    kwargs, _ = runtime_dependencies
    runtime = runtime_module.AgentRuntime(
        **{**kwargs, "config": config(kwargs, enabled=True, character_id="luotianyi")},
        minecraft_action_port=Port(),
    )
    try:
        assert runtime.get_agent("luotianyi")._stimulus_router.resolve(d.StimulusKind.MINECRAFT_OBSERVED) is not None
        assert runtime.get_agent("luotianyi")._action_router.resolve(d.ActionKind.MINECRAFT_ACTION) is not None
        with pytest.raises(KeyError):
            runtime.get_agent("miku")._stimulus_router.resolve(d.StimulusKind.MINECRAFT_OBSERVED)
        with pytest.raises(KeyError):
            runtime.get_agent("miku")._action_router.resolve(d.ActionKind.MINECRAFT_ACTION)
    finally:
        await runtime.shutdown()
