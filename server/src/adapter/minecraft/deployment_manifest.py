"""Strict, inactive parser for v2 Minecraft control deployment manifests.

This module validates an offline deployment record only.  It neither loads files
nor grants a transport, binding, permit, or other runtime capability.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Final

_MAX_MANIFEST_BYTES: Final = 4096
_ROLES: Final = frozenset(("SERVER_AUTHORITY", "CLIENT_CONTROLLER"))
_HASH_RE: Final = re.compile(r"[0-9a-f]{64}")
_ASCII_ID_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_KEYS: Final = (
    "schema",
    "security",
    "peerRole",
    "peerInstallId",
    "hubInstallId",
    "protocol",
    "postAuthProfile",
    "sessionKdfDomain",
    "frameMacDomain",
    "frameMaxUtf8Bytes",
    "payloadMaxRawBytes",
    "asciiIdMaxBytes",
    "outboundQueueFrames",
    "openTimeoutMs",
    "heartbeatIntervalMs",
    "heartbeatTimeoutMs",
    "routeCapacity",
    "routeTtlMs",
    "bindingPendingCapacity",
    "storeOutstandingCapacity",
    "modManifestSha256",
    "policyConfigSha256",
    "bindingManifestSha256",
)
_FIXED_STRINGS: Final = {
    "schema": "luosandbox.minecraft-control-deployment.v2",
    "security": "luosandbox.control-auth.v1",
    "protocol": "LUO_CONTROL_POSTAUTH_V2",
    "postAuthProfile": "LUO_CONTROL_POSTAUTH_V2",
    "sessionKdfDomain": "LUO_CONTROL_SESSION_KEY_V2",
    "frameMacDomain": "LUO_CONTROL_FRAME_V2",
}
_FIXED_NUMBERS: Final = {
    "frameMaxUtf8Bytes": 4096,
    "payloadMaxRawBytes": 2048,
    "asciiIdMaxBytes": 128,
    "outboundQueueFrames": 8,
    "openTimeoutMs": 5000,
    "heartbeatIntervalMs": 500,
    "heartbeatTimeoutMs": 2000,
    "routeCapacity": 2,
    "routeTtlMs": 5000,
    "bindingPendingCapacity": 8,
    "storeOutstandingCapacity": 2,
}


class DeploymentManifestError(ValueError):
    """Raised when an inactive deployment manifest violates its byte contract."""


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class DeploymentManifest:
    """The role-scoped deployment record and its canonical raw-byte hash."""

    schema: str
    security: str
    peer_role: str
    peer_install_id: str
    hub_install_id: str
    protocol: str
    post_auth_profile: str
    session_kdf_domain: str
    frame_mac_domain: str
    frame_max_utf8_bytes: int
    payload_max_raw_bytes: int
    ascii_id_max_bytes: int
    outbound_queue_frames: int
    open_timeout_ms: int
    heartbeat_interval_ms: int
    heartbeat_timeout_ms: int
    route_capacity: int
    route_ttl_ms: int
    binding_pending_capacity: int
    store_outstanding_capacity: int
    mod_manifest_sha256: str
    policy_config_sha256: str
    binding_manifest_sha256: str
    raw_bytes: bytes = field(init=False, repr=False)
    config_hash: bytes = field(init=False, repr=False)

    def __post_init__(self) -> None:
        _validate_values(self)
        raw_bytes = encode_deployment_manifest(self)
        object.__setattr__(self, "raw_bytes", raw_bytes)
        object.__setattr__(self, "config_hash", sha256(raw_bytes).digest())


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class TrustedDeploymentIdentity:
    """Provisioned identity and raw deployment-manifest digest, never manifest-derived."""

    peer_role: str
    peer_install_id: str
    hub_install_id: str
    expected_config_hash: bytes

    def __post_init__(self) -> None:
        if self.peer_role not in _ROLES or not all(
            _ascii_id(value) for value in (self.peer_install_id, self.hub_install_id)
        ):
            raise DeploymentManifestError("invalid trusted deployment identity")
        object.__setattr__(self, "expected_config_hash", _raw32(self.expected_config_hash, "config hash"))


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class TrustedDeploymentArtifacts:
    """Raw trusted artifacts whose hashes are bound by a deployment manifest."""

    mod_manifest_bytes: bytes
    policy_config_bytes: bytes
    binding_manifest_bytes: bytes

    def __post_init__(self) -> None:
        for field_name in ("mod_manifest_bytes", "policy_config_bytes", "binding_manifest_bytes"):
            object.__setattr__(self, field_name, _bytes(getattr(self, field_name), field_name))


def parse_deployment_manifest(raw_bytes: bytes) -> DeploymentManifest:
    """Parse exactly one canonical ASCII deployment manifest without normalizing it."""
    raw_bytes = _manifest_bytes(raw_bytes)
    try:
        decoded = raw_bytes.decode("ascii")
        values = json.loads(
            decoded,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, DeploymentManifestError) as error:
        raise DeploymentManifestError("invalid deployment manifest") from error
    if type(values) is not dict or tuple(values) != _KEYS:
        raise DeploymentManifestError("invalid deployment manifest keys")
    manifest = DeploymentManifest(**_from_json_values(values))
    if manifest.raw_bytes != raw_bytes:
        raise DeploymentManifestError("deployment manifest is not canonical")
    return manifest


def encode_deployment_manifest(manifest: DeploymentManifest) -> bytes:
    """Encode a manifest as its single permitted ASCII byte representation."""
    if not isinstance(manifest, DeploymentManifest):
        raise DeploymentManifestError("invalid deployment manifest")
    values = _to_json_values(manifest)
    return json.dumps(values, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"


def verify_trusted_deployment(
    manifest: DeploymentManifest,
    trusted_identity: TrustedDeploymentIdentity,
    trusted_artifacts: TrustedDeploymentArtifacts,
) -> None:
    """Check caller-provisioned identity, config hash, and raw artifact digests.

    Success only proves these offline inputs agree; it does not activate control.
    """
    if not isinstance(manifest, DeploymentManifest):
        raise DeploymentManifestError("invalid deployment manifest")
    if not isinstance(trusted_identity, TrustedDeploymentIdentity) or not isinstance(
        trusted_artifacts, TrustedDeploymentArtifacts
    ):
        raise DeploymentManifestError("invalid trusted deployment inputs")
    if (
        manifest.peer_role != trusted_identity.peer_role
        or manifest.peer_install_id != trusted_identity.peer_install_id
        or manifest.hub_install_id != trusted_identity.hub_install_id
        or manifest.config_hash != trusted_identity.expected_config_hash
        or _digest_hex(trusted_artifacts.mod_manifest_bytes) != manifest.mod_manifest_sha256
        or _digest_hex(trusted_artifacts.policy_config_bytes) != manifest.policy_config_sha256
        or _digest_hex(trusted_artifacts.binding_manifest_bytes) != manifest.binding_manifest_sha256
    ):
        raise DeploymentManifestError("trusted deployment verification failed")


def _from_json_values(values: dict[str, object]) -> dict[str, object]:
    return {
        "schema": values["schema"],
        "security": values["security"],
        "peer_role": values["peerRole"],
        "peer_install_id": values["peerInstallId"],
        "hub_install_id": values["hubInstallId"],
        "protocol": values["protocol"],
        "post_auth_profile": values["postAuthProfile"],
        "session_kdf_domain": values["sessionKdfDomain"],
        "frame_mac_domain": values["frameMacDomain"],
        "frame_max_utf8_bytes": values["frameMaxUtf8Bytes"],
        "payload_max_raw_bytes": values["payloadMaxRawBytes"],
        "ascii_id_max_bytes": values["asciiIdMaxBytes"],
        "outbound_queue_frames": values["outboundQueueFrames"],
        "open_timeout_ms": values["openTimeoutMs"],
        "heartbeat_interval_ms": values["heartbeatIntervalMs"],
        "heartbeat_timeout_ms": values["heartbeatTimeoutMs"],
        "route_capacity": values["routeCapacity"],
        "route_ttl_ms": values["routeTtlMs"],
        "binding_pending_capacity": values["bindingPendingCapacity"],
        "store_outstanding_capacity": values["storeOutstandingCapacity"],
        "mod_manifest_sha256": values["modManifestSha256"],
        "policy_config_sha256": values["policyConfigSha256"],
        "binding_manifest_sha256": values["bindingManifestSha256"],
    }


def _to_json_values(manifest: DeploymentManifest) -> dict[str, object]:
    return {
        "schema": manifest.schema,
        "security": manifest.security,
        "peerRole": manifest.peer_role,
        "peerInstallId": manifest.peer_install_id,
        "hubInstallId": manifest.hub_install_id,
        "protocol": manifest.protocol,
        "postAuthProfile": manifest.post_auth_profile,
        "sessionKdfDomain": manifest.session_kdf_domain,
        "frameMacDomain": manifest.frame_mac_domain,
        "frameMaxUtf8Bytes": manifest.frame_max_utf8_bytes,
        "payloadMaxRawBytes": manifest.payload_max_raw_bytes,
        "asciiIdMaxBytes": manifest.ascii_id_max_bytes,
        "outboundQueueFrames": manifest.outbound_queue_frames,
        "openTimeoutMs": manifest.open_timeout_ms,
        "heartbeatIntervalMs": manifest.heartbeat_interval_ms,
        "heartbeatTimeoutMs": manifest.heartbeat_timeout_ms,
        "routeCapacity": manifest.route_capacity,
        "routeTtlMs": manifest.route_ttl_ms,
        "bindingPendingCapacity": manifest.binding_pending_capacity,
        "storeOutstandingCapacity": manifest.store_outstanding_capacity,
        "modManifestSha256": manifest.mod_manifest_sha256,
        "policyConfigSha256": manifest.policy_config_sha256,
        "bindingManifestSha256": manifest.binding_manifest_sha256,
    }


def _validate_values(manifest: DeploymentManifest) -> None:
    strings = _to_json_values(manifest)
    if any(strings[key] != value for key, value in _FIXED_STRINGS.items()):
        raise DeploymentManifestError("invalid fixed deployment manifest value")
    if any(strings[key] != value or type(strings[key]) is not int for key, value in _FIXED_NUMBERS.items()):
        raise DeploymentManifestError("invalid fixed deployment manifest value")
    if manifest.peer_role not in _ROLES or not all(
        _ascii_id(value) for value in (manifest.peer_install_id, manifest.hub_install_id)
    ):
        raise DeploymentManifestError("invalid deployment manifest identity")
    if not all(
        type(value) is str and _HASH_RE.fullmatch(value) is not None
        for value in (
            manifest.mod_manifest_sha256,
            manifest.policy_config_sha256,
            manifest.binding_manifest_sha256,
        )
    ):
        raise DeploymentManifestError("invalid deployment manifest hash")


def _manifest_bytes(value: object) -> bytes:
    if type(value) is not bytes or not value or len(value) > _MAX_MANIFEST_BYTES:
        raise DeploymentManifestError("invalid deployment manifest bytes")
    return value


def _bytes(value: object, description: str) -> bytes:
    if type(value) is not bytes:
        raise DeploymentManifestError(f"invalid {description}")
    return value


def _raw32(value: object, description: str) -> bytes:
    value = _bytes(value, description)
    if len(value) != 32:
        raise DeploymentManifestError(f"invalid {description}")
    return value


def _ascii_id(value: object) -> bool:
    return type(value) is str and _ASCII_ID_RE.fullmatch(value) is not None


def _digest_hex(value: bytes) -> str:
    return sha256(value).hexdigest()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DeploymentManifestError("duplicate deployment manifest key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise DeploymentManifestError(f"invalid JSON constant: {value}")
