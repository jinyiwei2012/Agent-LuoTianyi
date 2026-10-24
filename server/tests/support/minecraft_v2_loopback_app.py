"""Test-only ASGI endpoint for real Java/Python v2 loopback evidence."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import importlib
import json
import os
import secrets
import struct
import sys
import tempfile
import time
import types
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketDisconnect

SERVER_ROOT = Path(__file__).resolve().parents[2]
SHUTDOWN_TOKEN_HEX_LENGTH = 64


def _install_controlled_packages() -> None:
    packages = {
        "src": SERVER_ROOT / "src",
        "src.adapter": SERVER_ROOT / "src/adapter",
        "src.adapter.minecraft": SERVER_ROOT / "src/adapter/minecraft",
    }
    for name, path in packages.items():
        module = types.ModuleType(name)
        module.__path__ = [str(path)]
        module.__package__ = name
        sys.modules[name] = module

    domain = types.ModuleType("src.domain")
    domain.__path__ = []
    domain.__package__ = "src.domain"
    sys.modules["src.domain"] = domain
    domain_agent = types.ModuleType("src.domain.agent")
    domain_agent.__package__ = "src.domain.agent"
    sys.modules["src.domain.agent"] = domain_agent
    domain.agent = domain_agent
    stage = types.ModuleType("src.stage")
    stage.__package__ = "src.stage"
    stage.WorldFactSink = object
    sys.modules["src.stage"] = stage


_install_controlled_packages()
control_auth = importlib.import_module("src.adapter.minecraft.control_auth")
current_roles = importlib.import_module("src.adapter.minecraft.current_roles")
deployment_acl = importlib.import_module("src.adapter.minecraft.deployment_acl")
deployment_files = importlib.import_module("src.adapter.minecraft.deployment_files")
deployment_manifest = importlib.import_module("src.adapter.minecraft.deployment_manifest")
postauth_frame = importlib.import_module("src.adapter.minecraft.postauth_frame")
postauth_keys = importlib.import_module("src.adapter.minecraft.postauth_keys")
postauth_session = importlib.import_module("src.adapter.minecraft.postauth_session")
transport_module = importlib.import_module("src.adapter.minecraft.transport")
wire = importlib.import_module("src.adapter.minecraft.wire")
typed_ingress = importlib.import_module("src.adapter.minecraft.typed_ingress")
epoch_store = importlib.import_module("src.adapter.minecraft.epoch_store")
epoch_platform = importlib.import_module("src.adapter.minecraft.epoch_platform")
epoch_worker = importlib.import_module("src.adapter.minecraft.epoch_worker")
control_binding = importlib.import_module("src.adapter.minecraft.control_binding")
v2_body_routes = importlib.import_module("src.adapter.minecraft.v2_body_routes")
v2_startup = importlib.import_module("src.adapter.minecraft.v2_startup")
v2_transport = importlib.import_module("src.adapter.minecraft.v2_transport")

TrustedDeploymentAclPolicy = deployment_acl.TrustedDeploymentAclPolicy
inspect_handle_security_descriptor = deployment_acl.inspect_handle_security_descriptor
DeploymentFileProvision = deployment_files.DeploymentFileProvision
TrustedDeploymentRoot = deployment_files.TrustedDeploymentRoot
_file_info = deployment_files._file_info
load_verified_deployment_filesystem = deployment_files.load_verified_deployment_filesystem
DeploymentManifest = deployment_manifest.DeploymentManifest
TrustedDeploymentIdentity = deployment_manifest.TrustedDeploymentIdentity
provision_v2_peer = v2_startup.provision_v2_peer
MinecraftV2AuthenticatedTransport = v2_transport.MinecraftV2AuthenticatedTransport

MOD = b"public-mod-manifest"
POLICY = b"public-policy-config"
BINDING = b"public-binding-manifest"
SERVER_KEY = bytes(range(32))
CLIENT_KEY = bytes(range(32, 64))
_MODULES = {
    "python:src/adapter/minecraft/deployment_files.py": deployment_files,
    "python:src/adapter/minecraft/deployment_acl.py": deployment_acl,
    "python:src/adapter/minecraft/deployment_manifest.py": deployment_manifest,
    "python:src/adapter/minecraft/v2_startup.py": v2_startup,
    "python:src/adapter/minecraft/control_auth.py": control_auth,
    "python:src/adapter/minecraft/current_roles.py": current_roles,
    "python:src/adapter/minecraft/postauth_keys.py": postauth_keys,
    "python:src/adapter/minecraft/postauth_frame.py": postauth_frame,
    "python:src/adapter/minecraft/postauth_session.py": postauth_session,
    "python:src/adapter/minecraft/transport.py": transport_module,
    "python:src/adapter/minecraft/wire.py": wire,
    "python:src/adapter/minecraft/typed_ingress.py": typed_ingress,
    "python:src/adapter/minecraft/epoch_store.py": epoch_store,
    "python:src/adapter/minecraft/epoch_platform.py": epoch_platform,
    "python:src/adapter/minecraft/epoch_worker.py": epoch_worker,
    "python:src/adapter/minecraft/control_binding.py": control_binding,
    "python:src/adapter/minecraft/v2_body_routes.py": v2_body_routes,
    "python:src/adapter/minecraft/v2_transport.py": v2_transport,
}

PROVISION_ID = "10000000-0000-4000-8000-000000000001"
BODY_ID = "70000000-0000-4000-8000-000000000001"


class _LockOnlyVerifiedPlatform:
    """TEST ONLY: native lock evidence, not production capability evidence."""

    reason = "LOCK_ONLY_NOT_PRODUCTION_CAPABILITY"

    def __init__(self) -> None:
        self._native = epoch_platform.WindowsEpochPlatform()

    def capability(self, directory: Path):
        facts = self._native.volume_facts(directory.parent if not directory.exists() else directory)
        verified = facts.local and facts.persistent_acls and not facts.reparse_point
        return epoch_store.PlatformCapability(verified, self.reason)

    def acquire_lock(self, path: Path):
        return self._native.acquire_lock(path)


class _PrepareRecorder:
    def __init__(self) -> None:
        self.sent: list[object] = []
        self.release = asyncio.Event()

    async def send(self, command) -> None:
        self.sent.append(command)
        await self.release.wait()

    async def revoke(self, command) -> None:
        return None


class _UnboundStage:
    character_id = "luotianyi"
    world_id = "world"

    def __init__(self) -> None:
        self.minecraft_observation_scope = None

    def bind_minecraft_observations(self, scope) -> None:
        self.minecraft_observation_scope = scope

    def unbind_minecraft_observations(self, scope) -> bool:
        self.minecraft_observation_scope = None
        return True


def _binding_policy():
    return control_binding.ControlBindingPolicy(
        actor_id="actor",
        world_id="world",
        body_id=BODY_ID,
        character_id="luotianyi",
        provision_id=PROVISION_ID,
        server_install_id="loopback-server",
        client_install_id="loopback-client",
    )


def _manifest(role: str, peer_id: str) -> DeploymentManifest:
    return DeploymentManifest(
        schema="luosandbox.minecraft-control-deployment.v2",
        security="luosandbox.control-auth.v1",
        peer_role=role,
        peer_install_id=peer_id,
        hub_install_id="loopback-hub",
        protocol="LUO_CONTROL_POSTAUTH_V2",
        post_auth_profile="LUO_CONTROL_POSTAUTH_V2",
        session_kdf_domain="LUO_CONTROL_SESSION_KEY_V2",
        frame_mac_domain="LUO_CONTROL_FRAME_V2",
        frame_max_utf8_bytes=4096,
        payload_max_raw_bytes=2048,
        ascii_id_max_bytes=128,
        outbound_queue_frames=8,
        open_timeout_ms=5000,
        heartbeat_interval_ms=500,
        heartbeat_timeout_ms=2000,
        route_capacity=2,
        route_ttl_ms=5000,
        binding_pending_capacity=8,
        store_outstanding_capacity=2,
        mod_manifest_sha256=sha256(MOD).hexdigest(),
        policy_config_sha256=sha256(POLICY).hexdigest(),
        binding_manifest_sha256=sha256(BINDING).hexdigest(),
    )


def _protect_owner_only(handle, owner_sid: bytes) -> None:
    ace_size = 8 + len(owner_sid)
    acl_size = 8 + ace_size
    acl = ctypes.create_string_buffer(
        struct.pack("<BBHHH", 2, 0, acl_size, 1, 0) + struct.pack("<BBHI", 0, 0, ace_size, 0x001F01FF) + owner_sid
    )
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.SetSecurityInfo.argtypes = (
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    advapi.SetSecurityInfo.restype = ctypes.c_uint
    if advapi.SetSecurityInfo(handle, 1, 0x4 | 0x80000000, None, None, ctypes.cast(acl, ctypes.c_void_p), None):
        raise RuntimeError("temporary ACL setup failed")


def _provision_peer(base: Path, role: str, peer_id: str, key: bytes):
    root = base / role.lower() / "trusted"
    root.mkdir(parents=True)
    manifest = _manifest(role, peer_id)
    paths = [root / name for name in ("deployment.json", "mod.json", "policy.json", "binding.json")]
    for path, raw in zip(paths, (manifest.raw_bytes, MOD, POLICY, BINDING)):
        path.write_bytes(raw)
    provision = DeploymentFileProvision(
        deployment_manifest_path=paths[0],
        mod_manifest_path=paths[1],
        policy_config_path=paths[2],
        binding_manifest_path=paths[3],
        trusted_identity=TrustedDeploymentIdentity(
            peer_role=role,
            peer_install_id=peer_id,
            hub_install_id="loopback-hub",
            expected_config_hash=manifest.config_hash,
        ),
    )
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_void_p,
    )
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    root_handle = kernel.CreateFileW(
        str(root),
        0x00020000 | 0x00040000 | 0x80,
        1,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    if root_handle in (None, ctypes.c_void_p(-1).value):
        raise RuntimeError("temporary root open failed")
    try:
        identity = _file_info(kernel, root_handle)
        descriptor = inspect_handle_security_descriptor(root_handle)
    finally:
        kernel.CloseHandle(root_handle)
    for path in paths:
        handle = kernel.CreateFileW(str(path), 0x00020000 | 0x00040000, 7, None, 3, 0x80 | 0x00200000, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            raise RuntimeError("temporary artifact open failed")
        try:
            _protect_owner_only(handle, descriptor.owner_sid)
        finally:
            kernel.CloseHandle(handle)
    root_handle = kernel.CreateFileW(
        str(root),
        0x00020000 | 0x00040000 | 0x80,
        1,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    try:
        _protect_owner_only(root_handle, descriptor.owner_sid)
    finally:
        kernel.CloseHandle(root_handle)
    verified = load_verified_deployment_filesystem(
        provision,
        TrustedDeploymentRoot(
            root_path=root,
            expected_volume_serial=identity.volume_serial,
            expected_file_index=identity.file_index,
            acl_policy=TrustedDeploymentAclPolicy(
                expected_owner_sid=descriptor.owner_sid,
                allowed_writer_sids=(descriptor.owner_sid,),
            ),
        ),
    )
    return provision_v2_peer(
        verified,
        expected_peer_role=role,
        expected_peer_install_id=peer_id,
        expected_hub_install_id="loopback-hub",
        expected_config_hash=manifest.config_hash,
        key_id=f"loopback-{role.lower()}",
        role_psk=key,
    )


def _utc_now() -> datetime:
    value = datetime.now(timezone.utc)
    return value.replace(microsecond=value.microsecond // 1000 * 1000)


def _write_receipt(receipt: dict[str, object]) -> None:
    path = Path(os.environ["LUOSANDBOX_LOOPBACK_SHUTDOWN_RECEIPT"])
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: C901
    temporary = tempfile.TemporaryDirectory(prefix="luosandbox-v2-loopback-")
    peers = (
        _provision_peer(Path(temporary.name), "SERVER_AUTHORITY", "loopback-server", SERVER_KEY),
        _provision_peer(Path(temporary.name), "CLIENT_CONTROLLER", "loopback-client", CLIENT_KEY),
    )
    mode = os.environ.get("LUOSANDBOX_LOOPBACK_MODE", "OBSERVATIONAL")
    worker = coordinator = commands = stage = platform = None
    epoch_directory = Path(temporary.name) / "epoch"
    if mode == "HEADLESS_ADMISSION":
        platform = _LockOnlyVerifiedPlatform()
        worker = epoch_worker.MinecraftEpochStoreWorker(
            epoch_worker.EpochWorkerConfig(
                mode="PROVISION",
                directory=epoch_directory.resolve(),
                platform=platform,
                provision_id=PROVISION_ID,
                body_id=BODY_ID,
            )
        )
        await worker.start()
        roles = current_roles.AuthenticatedControlRoleOwner(tuple(peer.context for peer in peers), "loopback-hub")
        commands, stage = _PrepareRecorder(), _UnboundStage()
        coordinator = control_binding.MinecraftControlBindingCoordinator(
            policy=_binding_policy(),
            role_owner=roles,
            epoch_store=None,
            epoch_worker=worker,
            commands=commands,
            stage=stage,
            monotonic_now=time.monotonic,
            utc_now=_utc_now,
        )
        transport = MinecraftV2AuthenticatedTransport(
            peers=peers,
            hub_install_id="loopback-hub",
            monotonic_millis=lambda: time.monotonic_ns() // 1_000_000,
            utc_now=_utc_now,
            role_owner=roles,
            binding_coordinator=coordinator,
            epoch_worker=worker,
        )
    elif mode == "OBSERVATIONAL":
        transport = MinecraftV2AuthenticatedTransport(
            peers=peers,
            hub_install_id="loopback-hub",
            monotonic_millis=lambda: time.monotonic_ns() // 1_000_000,
            utc_now=_utc_now,
        )
    else:
        raise RuntimeError("invalid loopback mode")
    app.state.transport = transport
    app.state.peers = peers
    app.state.mode = mode
    app.state.worker = worker
    app.state.coordinator = coordinator
    app.state.commands = commands
    app.state.stage = stage
    app.state.platform = platform
    app.state.epoch_directory = epoch_directory
    app.state.reopen = None
    app.state.completed = 0
    fatal = None
    try:
        yield
    except BaseException as error:
        fatal = error
    errors: list[str] = []
    sessions_drained = not transport.active_roles and transport.pending_count == 0
    candidates_hidden = transport._body_routes.candidates() == ()
    if not sessions_drained:
        errors.append("SESSIONS_NOT_DRAINED")
    if not candidates_hidden:
        errors.append("ROUTE_CANDIDATES_STILL_VISIBLE")
    worker_closed = worker is None
    worker_thread_dead = worker is None
    worker_outstanding = 0
    reopen = None
    if commands is not None:
        commands.release.set()
    if worker is not None:
        try:
            await worker.close()
            worker_closed = worker.state is epoch_worker.EpochWorkerState.CLOSED
            worker_thread_dead = worker._thread is None or not worker._thread.is_alive()
            worker_outstanding = worker.outstanding_count
            reopened = epoch_worker.MinecraftEpochStoreWorker(
                epoch_worker.EpochWorkerConfig(
                    mode="OPEN",
                    directory=epoch_directory.resolve(),
                    platform=platform,
                )
            )
            await reopened.start()
            try:
                handle = reopened.submit_preflight(
                    epoch_worker.EpochPreflightRequest(
                        provision_id=PROVISION_ID,
                        body_id=BODY_ID,
                        coordinator_generation=0,
                        route_id="30000000-0000-4000-8000-000000000003",
                        server_connection_id="40000000-0000-4000-8000-000000000004",
                        controller_connection_id="50000000-0000-4000-8000-000000000005",
                    )
                )
                await reopened.wait(handle)
                snapshot = reopened.take_preflight(handle)
                reopen = {
                    "expectedLastEpoch": snapshot.expected_last_epoch,
                    "expectedGeneration": snapshot.expected_generation,
                    "nextEpoch": snapshot.next_epoch,
                }
                reopened.mark_obsolete(snapshot)
            finally:
                await reopened.close()
        except BaseException:
            errors.append("WORKER_CLEANUP_OR_REOPEN_FAILED")
    peer_failures = 0
    for peer in peers:
        try:
            peer.close()
        except BaseException:
            peer_failures += 1
    if peer_failures:
        errors.append("PEER_CLOSE_FAILED")
    temporary_succeeded = False
    try:
        temporary.cleanup()
        temporary_succeeded = True
    except BaseException:
        errors.append("TEMPORARY_CLEANUP_FAILED")
    receipt = {
        "schema": "luosandbox.v2-loopback-shutdown.v1",
        "nonce": os.environ["LUOSANDBOX_LOOPBACK_SHUTDOWN_NONCE"],
        "shutdownComplete": fatal is None and not errors,
        "sessionsDrained": sessions_drained,
        "peerCloseAttempts": len(peers),
        "peerCloseFailures": peer_failures,
        "temporaryCleanupAttempted": True,
        "temporaryCleanupSucceeded": temporary_succeeded,
        "cleanupErrors": errors,
        "workerClosed": worker_closed,
        "workerThreadDead": worker_thread_dead,
        "workerOutstandingCount": worker_outstanding,
        "epochReopen": reopen,
    }
    _write_receipt(receipt)
    app.state.shutdown_token = None
    if fatal is not None:
        raise fatal
    if errors:
        raise RuntimeError("loopback cleanup failed")


app = FastAPI(lifespan=lifespan)


@app.websocket("/minecraft/v2/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    if websocket.client is None or websocket.client.host not in ("127.0.0.1", "::1"):
        await websocket.close(code=1008, reason="FEATURE_DISABLED")
        return
    offered = websocket.headers.get("sec-websocket-protocol", "").split(",")
    if "LUO_CONTROL_POSTAUTH_V2" not in {item.strip() for item in offered}:
        await websocket.close(code=1008, reason="FEATURE_DISABLED")
        return
    try:
        await app.state.transport.handle(websocket)
    except WebSocketDisconnect:
        pass
    finally:
        app.state.completed += 1


@app.get("/minecraft/v2/test-status")
async def status() -> dict[str, object]:
    transport = app.state.transport
    paths = {name: Path(module.__file__) for name, module in _MODULES.items()}
    paths["python:tests/support/minecraft_v2_loopback_app.py"] = Path(__file__)
    return {
        "enabled": True,
        "routeRegistered": True,
        "activeRoles": list(transport.active_roles),
        "pendingCount": transport.pending_count,
        "completedSessions": app.state.completed,
        "applicationFrames": 0,
        "loadedModuleHashes": {name: sha256(path.read_bytes()).hexdigest() for name, path in paths.items()},
        "loadedModulePaths": {name: str(path.resolve()) for name, path in paths.items()},
    }


@app.get("/minecraft/v2/test-route-status")
async def route_status() -> dict[str, object]:
    transport = app.state.transport
    candidates = transport._body_routes.candidates()
    expected = (
        b'{"security":"luosandbox.control-auth.v1","kind":"BODY_BIND_NOTICE",'
        b'"serverBootId":"50000000-0000-4000-8000-000000000001",'
        b'"gameConnectionId":"60000000-0000-4000-8000-000000000001",'
        b'"bodyId":"70000000-0000-4000-8000-000000000001",'
        b'"gameNonce":"AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"}'
    )
    return {
        "nonAuthoritativeCandidateCount": len(candidates),
        "routeRawPayloadMatched": any(record.payload == expected for record in transport._body_routes._routes.values()),
        "coordinatorPresent": transport._binding is not None,
    }


@app.get("/minecraft/v2/test-verified-proof-status")
async def verified_proof_status() -> dict[str, object]:
    transport = app.state.transport
    candidates = transport._body_routes.candidates()
    payloads = [record.payload for record in transport._body_routes._routes.values()]
    return {
        "nonAuthoritativeCandidateCount": len(candidates),
        "routeNoticeStructureValid": all(_valid_notice(payload) for payload in payloads),
        "routeNoticeSha256": [sha256(payload).hexdigest() for payload in payloads[:2]],
        "coordinatorPresent": transport._binding is not None,
    }


@app.get("/minecraft/v2/test-headless-admission-status")
async def headless_admission_status() -> dict[str, object]:
    if app.state.mode != "HEADLESS_ADMISSION":
        return {"mode": app.state.mode}
    transport = app.state.transport
    coordinator = app.state.coordinator
    commands = app.state.commands
    stage = app.state.stage
    pending = list(coordinator._pending.values())
    tickets = list(coordinator._v2_tickets.values())
    sent_types = [type(command).__name__ for command in commands.sent]
    default_capability = epoch_store._UnsupportedPlatform().capability(app.state.epoch_directory)
    return {
        "mode": app.state.mode,
        "activeRoles": list(transport.active_roles),
        "nonAuthoritativeCandidateCount": len(transport._body_routes.candidates()),
        "admissionCount": len(transport._admissions),
        "coordinatorPendingCount": coordinator.pending_count,
        "ticketCount": len(tickets),
        "prepareCount": sent_types.count("BindingPrepare"),
        "commandTypes": sent_types,
        "workerOutstandingCount": app.state.worker.outstanding_count,
        "workerState": app.state.worker.state.value,
        "pendingPhase": pending[0].phase.value if len(pending) == 1 else None,
        "pendingCommitted": pending[0].committed is not None if len(pending) == 1 else None,
        "activePresent": coordinator.active is not None,
        "ingressPresent": coordinator.ingress is not None,
        "stageScopePresent": stage.minecraft_observation_scope is not None,
        "bindingCommitCount": sent_types.count("BindingCommit"),
        "platformReason": app.state.platform.reason,
        "productionCapabilityVerified": default_capability.verified,
    }


def _valid_notice(raw: bytes) -> bool:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, TypeError):
        return False
    return (
        type(value) is dict
        and set(value)
        == {
            "security",
            "kind",
            "serverBootId",
            "gameConnectionId",
            "bodyId",
            "gameNonce",
        }
        and value["security"] == "luosandbox.control-auth.v1"
        and value["kind"] == "BODY_BIND_NOTICE"
        and all(type(item) is str for item in value.values())
    )


@app.post("/minecraft/v2/test-shutdown")
async def shutdown(request: Request) -> JSONResponse:
    if (
        request.client is None
        or request.client.host not in ("127.0.0.1", "::1")
        or not _shutdown_token_matches(request.headers.get("x-loopback-shutdown"), app.state.shutdown_token)
    ):
        return JSONResponse({"accepted": False}, status_code=403)
    app.state.server.should_exit = True
    return JSONResponse({"accepted": True}, status_code=202)


def _shutdown_token_matches(candidate: object, expected: object) -> bool:
    if (
        type(candidate) is not str
        or type(expected) is not str
        or len(candidate) != SHUTDOWN_TOKEN_HEX_LENGTH
        or len(expected) != SHUTDOWN_TOKEN_HEX_LENGTH
        or any(character not in "0123456789abcdef" for character in candidate)
        or any(character not in "0123456789abcdef" for character in expected)
    ):
        return False
    return secrets.compare_digest(candidate, expected)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", choices=("127.0.0.1",), required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    import uvicorn

    token = os.environ.pop("LUOSANDBOX_LOOPBACK_SHUTDOWN_TOKEN")
    if (
        not _shutdown_token_matches(token, token)
        or not os.environ.get("LUOSANDBOX_LOOPBACK_SHUTDOWN_RECEIPT")
        or not os.environ.get("LUOSANDBOX_LOOPBACK_SHUTDOWN_NONCE")
    ):
        raise RuntimeError("loopback shutdown configuration missing")
    config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        workers=1,
        proxy_headers=False,
        ws_max_size=4096,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    app.state.server = server
    app.state.shutdown_token = token
    server.run()
    return 0 if server.started and not server.force_exit else 1


if __name__ == "__main__":
    raise SystemExit(main())
