from dataclasses import FrozenInstanceError
from hashlib import sha256

import pytest

from src.adapter.minecraft.deployment_manifest import (
    DeploymentManifest,
    DeploymentManifestError,
    TrustedDeploymentArtifacts,
    TrustedDeploymentIdentity,
    encode_deployment_manifest,
    parse_deployment_manifest,
    verify_trusted_deployment,
)

MOD = b"public-mod-manifest"
POLICY = b"public-policy-config"
BINDING = b"public-binding-manifest"


def digest(value: bytes) -> str:
    return sha256(value).hexdigest()


def manifest(**changes: object) -> DeploymentManifest:
    values: dict[str, object] = {
        "schema": "luosandbox.minecraft-control-deployment.v2",
        "security": "luosandbox.control-auth.v1",
        "peer_role": "SERVER_AUTHORITY",
        "peer_install_id": "server-test-1",
        "hub_install_id": "hub-test-1",
        "protocol": "LUO_CONTROL_POSTAUTH_V2",
        "post_auth_profile": "LUO_CONTROL_POSTAUTH_V2",
        "session_kdf_domain": "LUO_CONTROL_SESSION_KEY_V2",
        "frame_mac_domain": "LUO_CONTROL_FRAME_V2",
        "frame_max_utf8_bytes": 4096,
        "payload_max_raw_bytes": 2048,
        "ascii_id_max_bytes": 128,
        "outbound_queue_frames": 8,
        "open_timeout_ms": 5000,
        "heartbeat_interval_ms": 500,
        "heartbeat_timeout_ms": 2000,
        "route_capacity": 2,
        "route_ttl_ms": 5000,
        "binding_pending_capacity": 8,
        "store_outstanding_capacity": 2,
        "mod_manifest_sha256": digest(MOD),
        "policy_config_sha256": digest(POLICY),
        "binding_manifest_sha256": digest(BINDING),
    }
    values.update(changes)
    return DeploymentManifest(**values)  # type: ignore[arg-type]


def trusted(value: DeploymentManifest) -> tuple[TrustedDeploymentIdentity, TrustedDeploymentArtifacts]:
    return (
        TrustedDeploymentIdentity(
            peer_role=value.peer_role,
            peer_install_id=value.peer_install_id,
            hub_install_id=value.hub_install_id,
            expected_config_hash=value.config_hash,
        ),
        TrustedDeploymentArtifacts(
            mod_manifest_bytes=MOD,
            policy_config_bytes=POLICY,
            binding_manifest_bytes=BINDING,
        ),
    )


def test_round_trip_is_exact_ascii_23_key_canonical_bytes():
    value = manifest()
    raw = encode_deployment_manifest(value)
    parsed = parse_deployment_manifest(raw)

    assert raw == value.raw_bytes == parsed.raw_bytes
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1
    assert raw.decode("ascii")
    assert len(__import__("json").loads(raw)) == 23
    assert parsed.config_hash == sha256(raw).digest()


@pytest.mark.parametrize(
    "raw",
    (
        b"\xef\xbb\xbf{}\n",
        b'{"schema":"luosandbox.minecraft-control-deployment.v2"}\r\n',
        b"{}\n\n",
        b" {}\n",
        b"{} \n",
        b"{}",
        b"\xff\n",
    ),
)
def test_parser_rejects_noncanonical_bytes(raw: bytes):
    with pytest.raises(DeploymentManifestError):
        parse_deployment_manifest(raw)


def test_parser_rejects_all_structural_byte_mutations_and_reordering():
    raw = manifest().raw_bytes
    structural_indexes = (index for index, value in enumerate(raw) if value in b'{}":,\n')
    for index in structural_indexes:
        original = raw[index]
        changed = b"X" if original != ord("X") else b"Y"
        with pytest.raises(DeploymentManifestError):
            parse_deployment_manifest(raw[:index] + changed + raw[index + 1 :])

    reordered = raw.replace(b'"schema":', b'"temporary":', 1).replace(b'"security":', b'"schema":', 1)
    reordered = reordered.replace(b'"temporary":', b'"security":', 1)
    with pytest.raises(DeploymentManifestError):
        parse_deployment_manifest(reordered)


@pytest.mark.parametrize(
    "replacement",
    (
        b"true",
        b"1.0",
        b'"4096"',
    ),
)
def test_fixed_numeric_type_substitutions_are_rejected(replacement: bytes):
    raw = manifest().raw_bytes.replace(b"4096", replacement, 1)
    with pytest.raises(DeploymentManifestError):
        parse_deployment_manifest(raw)


def test_duplicate_unknown_missing_escape_and_bad_id_are_rejected():
    raw = manifest().raw_bytes
    variants = (
        raw.replace(b'"schema"', b'"unknown"', 1),
        raw.replace(b',"security":', b",", 1),
        raw.replace(b'{"schema":', b'{"schema":"x","schema":', 1),
        raw.replace(b"server-test-1", b"server\\u002dtest-1", 1),
        raw.replace(b"server-test-1", b"server\x00test-1", 1),
    )
    for invalid in variants:
        with pytest.raises(DeploymentManifestError):
            parse_deployment_manifest(invalid)


@pytest.mark.parametrize(
    "invalid_id",
    (
        " space",
        'quote"',
        "back\\slash",
        "tab\t",
        "line\n",
        "control\x1f",
        "-lead",
        "_lead",
        "at@",
        "equals=",
        "semicolon;",
        "a" * 129,
    ),
)
def test_manifest_and_trusted_identity_reject_noncanonical_ascii_ids(invalid_id: str):
    with pytest.raises(DeploymentManifestError):
        manifest(peer_install_id=invalid_id)
    with pytest.raises(DeploymentManifestError):
        TrustedDeploymentIdentity(
            peer_role="SERVER_AUTHORITY",
            peer_install_id=invalid_id,
            hub_install_id="hub-test-1",
            expected_config_hash=b"x" * 32,
        )


def test_ascii_id_accepts_one_alphanumeric_and_128_byte_allowed_punctuation():
    assert manifest(peer_install_id="a").peer_install_id == "a"
    maximum = "a" + "A9._:/-" * 18 + "A"
    assert len(maximum) == 128
    assert manifest(peer_install_id=maximum, hub_install_id=maximum).peer_install_id == maximum
    identity = TrustedDeploymentIdentity(
        peer_role="SERVER_AUTHORITY",
        peer_install_id=maximum,
        hub_install_id=maximum,
        expected_config_hash=b"x" * 32,
    )
    assert identity.peer_install_id == maximum


def test_role_scoped_hashes_are_isolated_and_raw_whitespace_changes_hash_but_is_rejected():
    server = manifest(peer_role="SERVER_AUTHORITY", peer_install_id="server-test-1")
    client = manifest(peer_role="CLIENT_CONTROLLER", peer_install_id="client-test-1")
    assert server.config_hash != client.config_hash
    assert sha256(server.raw_bytes + b" ").digest() != server.config_hash
    with pytest.raises(DeploymentManifestError):
        parse_deployment_manifest(server.raw_bytes + b" ")


def test_trusted_verification_requires_identity_config_hash_and_each_raw_artifact():
    value = manifest()
    identity, artifacts = trusted(value)
    assert verify_trusted_deployment(value, identity, artifacts) is None

    mismatched_identities = (
        TrustedDeploymentIdentity(
            peer_role="CLIENT_CONTROLLER",
            peer_install_id="server-test-1",
            hub_install_id="hub-test-1",
            expected_config_hash=value.config_hash,
        ),
        TrustedDeploymentIdentity(
            peer_role=value.peer_role,
            peer_install_id=value.peer_install_id,
            hub_install_id=value.hub_install_id,
            expected_config_hash=b"x" * 32,
        ),
    )
    for mismatch in mismatched_identities:
        with pytest.raises(DeploymentManifestError):
            verify_trusted_deployment(value, mismatch, artifacts)

    for field in ("mod_manifest_bytes", "policy_config_bytes", "binding_manifest_bytes"):
        changed = {
            "mod_manifest_bytes": MOD,
            "policy_config_bytes": POLICY,
            "binding_manifest_bytes": BINDING,
        }
        changed[field] += b"-mismatch"
        with pytest.raises(DeploymentManifestError):
            verify_trusted_deployment(value, identity, TrustedDeploymentArtifacts(**changed))


def test_models_are_immutable_redacted_and_have_no_activation_api():
    value = manifest()
    identity, artifacts = trusted(value)
    for item in (value, identity, artifacts):
        assert repr(item) != "" and digest(MOD) not in repr(item)
    with pytest.raises((AttributeError, FrozenInstanceError)):
        value.peer_role = "CLIENT_CONTROLLER"  # type: ignore[misc]
    assert not any("activate" in name or "capability" in name for name in dir(value))
