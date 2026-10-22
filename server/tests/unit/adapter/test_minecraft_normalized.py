"""Closed typed Minecraft request matrix, independent of transport/auth."""

import math

import pytest

from src.domain.agent.minecraft import (
    MinecraftActionRequest,
    MinecraftBodyState,
    MinecraftBoundScope,
    MinecraftIntent,
    MinecraftPosition,
)

SCOPE = MinecraftBoundScope(world_id="world", body_id="body", session_epoch=1)
STATE = MinecraftBodyState(
    dimension_id="minecraft:overworld", position=MinecraftPosition(x=1.0, y=64.0, z=2.0), yaw=0.0, pitch=0.0
)


def request(intent, **changes):
    values = dict(
        request_id="request",
        scope=SCOPE,
        intent=intent,
        basis_observation_id=None,
        public_message=None,
        target=None,
        maximum_distance=None,
        target_request_id=None,
    )
    values.update(changes)
    return MinecraftActionRequest(**values)


@pytest.mark.parametrize(
    "intent,changes",
    [
        (MinecraftIntent.QUERY, {}),
        (MinecraftIntent.PUBLIC_SAY, {"basis_observation_id": "obs", "public_message": "hello"}),
        (MinecraftIntent.LOOK, {"basis_observation_id": "obs", "target": STATE}),
        (MinecraftIntent.BOUNDED_WALK, {"basis_observation_id": "obs", "target": STATE, "maximum_distance": 4}),
        (MinecraftIntent.STOP, {"target_request_id": "prior"}),
    ],
)
def test_closed_intent_matrix_accepts_only_each_valid_shape(intent, changes):
    assert request(intent, **changes).intent is intent


@pytest.mark.parametrize("intent", list(MinecraftIntent))
def test_closed_intent_matrix_rejects_cross_fields(intent):
    with pytest.raises(ValueError):
        request(intent, public_message="wrong")


@pytest.mark.parametrize("message", ["", "/command", " \u2003/command", "bad\ntext", "bad\0text", "x" * 257])
def test_public_say_rejects_unsafe_content(message):
    with pytest.raises(ValueError):
        request(MinecraftIntent.PUBLIC_SAY, basis_observation_id="obs", public_message=message)


@pytest.mark.parametrize("value", [True, math.nan, math.inf, -math.inf, 30000001])
def test_coordinates_reject_bool_nonfinite_and_outside_world(value):
    with pytest.raises(ValueError):
        MinecraftPosition(x=value, y=0, z=0)


def test_scope_rejects_bool_epoch():
    with pytest.raises(ValueError):
        MinecraftBoundScope(world_id="world", body_id="body", session_epoch=True)


@pytest.mark.parametrize("basis", [None, "obs"])
def test_query_allows_optional_nonblank_basis(basis):
    assert request(MinecraftIntent.QUERY, basis_observation_id=basis).basis_observation_id == basis


@pytest.mark.parametrize("intent", [MinecraftIntent.QUERY, MinecraftIntent.STOP])
def test_query_and_stop_reject_empty_basis(intent):
    changes = {"basis_observation_id": ""}
    if intent is MinecraftIntent.STOP:
        changes["target_request_id"] = "prior"
    with pytest.raises(ValueError):
        request(intent, **changes)
