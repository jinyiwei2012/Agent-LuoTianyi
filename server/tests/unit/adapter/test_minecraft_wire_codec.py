"""Strict decoder regression tests against the pinned Minecraft v1 mirror."""

import hashlib
import json
from pathlib import Path

import pytest

from src.adapter.minecraft.wire import MinecraftWireError, WireErrorCategory, decode_message, encode_message

FIXTURES = Path(__file__).parents[2] / "fixtures" / "minecraft_contract_v1"


def test_pinned_manifest_hashes_match_mirrored_fixture_bytes():
    manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["specSha256"] == "7ee44ed20446533628e745ba591e14392af9f1b8e7ba44c2155b3bf72d34f1cb"
    assert hashlib.sha256((FIXTURES / "schema.json").read_bytes()).hexdigest() == manifest["schemaSha256"]
    for item in manifest["fixtures"]:
        assert hashlib.sha256((FIXTURES / item["path"]).read_bytes()).hexdigest() == item["sha256"]


def test_all_pinned_fixture_expectations_are_decoded_or_rejected():
    manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    for item in manifest["fixtures"]:
        data = (FIXTURES / item["path"]).read_bytes()
        if item["expectedResult"] == "accept":
            decoded = decode_message(data)
            assert decoded.type == item["type"]
            assert decode_message(encode_message(decoded)) == decoded
        else:
            with pytest.raises(MinecraftWireError):
                decode_message(data)
    for item in manifest["rawFixtures"]:
        path = FIXTURES / item["path"]
        source = path.read_bytes()
        raw = bytes.fromhex(source.decode("ascii").strip()) if path.suffix == ".hex" else source
        assert hashlib.sha256(source).hexdigest() == item["sha256"]
        with pytest.raises(MinecraftWireError):
            decode_message(raw)


@pytest.mark.parametrize(
    "data",
    [
        b'{"a":1,"a":2}',
        b"\xef\xbb\xbf{}",
        b'{"x":"\\ud800"}',
        b'{"protocol":"luosandbox.minecraft.v1","type":"request","actorId":"agent","worldId":"minecraft:overworld","bodyId":"11111111-1111-4111-8111-000000000001","sessionId":"00000002-1111-4111-8111-000000000002","sessionEpoch":1e0,"messageId":"00000006-1111-4111-8111-000000000006","timestamp":"2026-10-23T00:00:00.000Z","payload":{"deadline":"2026-10-23T00:00:05.000Z","basis":null,"intent":{"kind":"QUERY","queryKind":"READ_ONLY_SUMMARY"}}}',
    ],
)
def test_lexical_rejections(data):
    with pytest.raises(MinecraftWireError) as error:
        decode_message(data)
    assert error.value.category is WireErrorCategory.LEXICAL


def test_payload_is_recursively_immutable_and_encoder_thaws_it():
    envelope = decode_message((FIXTURES / "valid" / "observation_public_current.json").read_bytes())
    with pytest.raises(TypeError):
        envelope.payload["freshness"] = "STALE"
    with pytest.raises(TypeError):
        envelope.payload["state"]["yaw"] = 3
    assert decode_message(encode_message(envelope)) == envelope


@pytest.mark.parametrize("suffix", [b"0" * 65, b'"' + b"x" * 16385 + b'"', b"{" * 13 + b"}" * 13])
def test_parser_limits_reject_before_semantic_decode(suffix):
    with pytest.raises(MinecraftWireError) as error:
        decode_message(suffix)
    assert error.value.category is WireErrorCategory.LEXICAL


def _json_fixture(name):
    return json.loads((FIXTURES / "valid" / name).read_text(encoding="utf-8"))


def test_f64_decimal_forms_normalize_and_encoder_emits_positive_zero():
    value = _json_fixture("request_walk.json")
    value["payload"]["intent"]["target"] = {"x": -0.0, "y": 0.5, "z": 1e-1}
    decoded = decode_message(json.dumps(value, separators=(",", ":")).encode())
    target = decoded.payload["intent"]["target"]
    assert target == {"x": 0.0, "y": 0.5, "z": 0.1}
    encoded = encode_message(decoded)
    assert b'"x":0' in encoded
    assert decode_message(encoded) == decoded


@pytest.mark.parametrize(
    "name, mutate",
    [
        ("request_say.json", lambda item: item["payload"].update(basis=None)),
        ("admission_rejected.json", lambda item: item["payload"].update(reason="TARGET_TERMINAL")),
        (
            "cancel_result_requested.json",
            lambda item: item["payload"].update(result="ALREADY_TERMINAL", reason="BACKPRESSURE"),
        ),
        (
            "receipt_succeeded.json",
            lambda item: item["payload"].update(
                outcome={"kind": "UNDETERMINED", "effect": "COMPLETE", "state": None, "observationId": None}
            ),
        ),
        ("receipt_succeeded.json", lambda item: item["payload"]["stamp"].update(evidence=[])),
    ],
)
def test_cross_field_reason_basis_and_receipt_invariants(name, mutate):
    value = _json_fixture(name)
    mutate(value)
    with pytest.raises(MinecraftWireError):
        decode_message(json.dumps(value, separators=(",", ":")).encode())


@pytest.mark.parametrize("value", ["Minecraft:overworld", "minecraft:Overworld", "minecraft:", "minecraft:bad:again"])
def test_dimension_resource_grammar_is_strict(value):
    message = _json_fixture("request_look.json")
    message["payload"]["intent"]["dimensionId"] = value
    with pytest.raises(MinecraftWireError):
        decode_message(json.dumps(message, separators=(",", ":")).encode())
