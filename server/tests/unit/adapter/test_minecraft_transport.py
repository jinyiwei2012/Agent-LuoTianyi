import asyncio
import inspect
import json
import math
from datetime import datetime, timezone

import pytest

from src.adapter.minecraft.control_auth import ControlAuthContext, b32_encode, compute_proof
from src.adapter.minecraft.transport import AllowedPeer, MinecraftAuthenticatedTransport

NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)
KEY = bytes(range(32))


class Clock:
    def __init__(self):
        self.value = 10.0

    def __call__(self):
        return self.value


class Socket:
    def __init__(self, messages):
        self.messages = asyncio.Queue()
        for item in messages:
            self.messages.put_nowait(item)
        self.sent = []
        self.closed = None
        self.headers = {}
        self.client = None
        self.block_send_at = None
        self.send_entered = asyncio.Event()
        self.release_send = asyncio.Event()

    async def accept(self, *, subprotocol=None):
        self.accepted = subprotocol

    async def receive_text(self):
        return await self.messages.get()

    async def send_text(self, data):
        self.send_entered.set()
        if self.block_send_at == len(self.sent):
            await self.release_send.wait()
        self.sent.append(json.loads(data))

    async def close(self, code=1000, reason=None):
        self.closed = (code, reason)


def context(role="SERVER_AUTHORITY", install="server-install"):
    return ControlAuthContext(
        peer_role=role,
        peer_install_id=install,
        hub_install_id="hub",
        key_id="key",
        protocol="luosandbox.minecraft.v1",
        mod_hash=b"m" * 32,
        config_hash=b"c" * 32,
        binding_hash=b"b" * 32,
    )


def hello(selected=None):
    selected = selected or context()
    return json.dumps(
        {
            "security": "luosandbox.control-auth.v1",
            "kind": "HELLO",
            "peerRole": selected.peer_role,
            "peerInstallId": selected.peer_install_id,
            "hubInstallId": selected.hub_install_id,
            "keyId": selected.key_id,
            "protocol": selected.protocol,
            "modHash": b32_encode(selected.mod_hash),
            "configHash": b32_encode(selected.config_hash),
            "bindingHash": b32_encode(selected.binding_hash),
            "peerNonce": b32_encode(b"p" * 32),
        },
        separators=(",", ":"),
    )


def transport(clock=None, **changes):
    clock = clock or Clock()
    return MinecraftAuthenticatedTransport(
        peers=(AllowedPeer(context(), KEY), AllowedPeer(context("CLIENT_CONTROLLER", "client-install"), b"c" * 32)),
        hub_install_id="hub",
        monotonic_now=clock,
        utc_now=lambda: NOW,
        nonce=lambda: b"h" * 32,
        challenge_id=lambda: "10000000-0000-0000-0000-000000000001",
        **changes,
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("handshake_seconds", 300),
        ("heartbeat_seconds", 60),
        ("control_lost_seconds", 3600),
        ("handshake_seconds", True),
        ("heartbeat_seconds", float("nan")),
        ("control_lost_seconds", float("inf")),
        ("handshake_seconds", -1),
    ],
)
def test_security_timeouts_are_not_publicly_configurable(name, value):
    assert name not in inspect.signature(MinecraftAuthenticatedTransport).parameters
    with pytest.raises(TypeError):
        transport(**{name: value})


def test_frozen_timeout_constants_are_exact():
    from src.adapter.minecraft import transport as module

    assert module._HEARTBEAT_SECONDS == 0.5
    assert module._CONTROL_LOST_SECONDS == 2.0
    assert math.isfinite(module._HEARTBEAT_SECONDS) and math.isfinite(module._CONTROL_LOST_SECONDS)


@pytest.mark.asyncio
async def test_valid_handshake_emits_challenge_ready_then_effect_message_is_feature_disabled():
    selected = context()
    socket = Socket([hello(selected)])
    owner = transport()
    task = asyncio.create_task(owner.handle(socket))
    while not socket.sent:
        await asyncio.sleep(0)
    challenge = socket.sent[0]
    proof = compute_proof(
        KEY,
        "PEER_PROOF",
        selected,
        b"p" * 32,
        b"h" * 32,
        challenge["challengeId"],
        NOW,
        datetime.fromisoformat(challenge["expiresAt"].replace("Z", "+00:00")),
    )
    await socket.messages.put(
        json.dumps(
            {
                "security": "luosandbox.control-auth.v1",
                "kind": "PROOF",
                "challengeId": challenge["challengeId"],
                "peerProof": proof,
            }
        )
    )
    while len(socket.sent) < 2:
        await asyncio.sleep(0)
    assert socket.sent[1]["kind"] == "READY"
    await socket.messages.put(json.dumps({"security": "luosandbox.control-auth.v1", "kind": "BINDING_ACTIVE"}))
    await task
    assert socket.closed == (1008, "FEATURE_DISABLED")
    assert owner.active_roles == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_hello",
    [
        lambda: hello().replace('"server-install"', '"wrong-install"'),
        lambda: hello()[:-1] + ',"extra":1}',
        lambda: '{"security":"luosandbox.control-auth.v1","kind":"HELLO","kind":"HELLO"}',
        lambda: "x" * 4097,
    ],
)
async def test_invalid_hello_forms_close_fail_closed(bad_hello):
    socket = Socket([bad_hello()])
    await transport().handle(socket)
    assert socket.closed == (1008, "FEATURE_DISABLED")


@pytest.mark.asyncio
async def test_wrong_proof_is_consumed_and_replay_not_authenticated():
    owner = transport()
    socket = Socket([hello()])
    task = asyncio.create_task(owner.handle(socket))
    while not socket.sent:
        await asyncio.sleep(0)
    challenge = socket.sent[0]
    proof = json.dumps(
        {
            "security": "luosandbox.control-auth.v1",
            "kind": "PROOF",
            "challengeId": challenge["challengeId"],
            "peerProof": b32_encode(b"x" * 32),
        }
    )
    await socket.messages.put(proof)
    await task
    assert socket.closed == (1008, "FEATURE_DISABLED")
    replay = Socket([hello(), proof])
    await owner.handle(replay)
    assert replay.closed == (1008, "FEATURE_DISABLED")


@pytest.mark.asyncio
async def test_heartbeat_sequence_regression_closes_and_releases_role():
    selected = context()
    socket = Socket([hello(selected)])
    owner = transport()
    task = asyncio.create_task(owner.handle(socket))
    while not socket.sent:
        await asyncio.sleep(0)
    challenge = socket.sent[0]
    proof = compute_proof(
        KEY,
        "PEER_PROOF",
        selected,
        b"p" * 32,
        b"h" * 32,
        challenge["challengeId"],
        NOW,
        datetime.fromisoformat(challenge["expiresAt"].replace("Z", "+00:00")),
    )
    await socket.messages.put(
        json.dumps(
            {
                "security": "luosandbox.control-auth.v1",
                "kind": "PROOF",
                "challengeId": challenge["challengeId"],
                "peerProof": proof,
            }
        )
    )
    while len(socket.sent) < 2:
        await asyncio.sleep(0)
    heartbeat = json.dumps({"security": "luosandbox.control-auth.v1", "kind": "HEARTBEAT", "sequence": 1})
    await socket.messages.put(heartbeat)
    await socket.messages.put(heartbeat)
    await task
    assert owner.active_roles == ()


@pytest.mark.asyncio
async def test_same_role_is_reserved_before_challenge_send_and_other_role_can_handshake():
    owner = transport()
    first = Socket([hello()])
    first.block_send_at = 0
    first_task = asyncio.create_task(owner.handle(first))
    await first.send_entered.wait()
    second = Socket([hello()])
    await owner.handle(second)
    assert second.closed == (1008, "FEATURE_DISABLED")
    client = Socket([hello(context("CLIENT_CONTROLLER", "client-install"))])
    client_task = asyncio.create_task(owner.handle(client))
    while not client.sent:
        await asyncio.sleep(0)
    assert client.sent[0]["kind"] == "CHALLENGE"
    client_task.cancel()
    first.release_send.set()
    first_task.cancel()
    await asyncio.gather(first_task, client_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_five_silent_connections_reserve_four_slots_and_release_allows_new_slot():
    owner = transport()
    sockets = [Socket([]) for _ in range(4)]
    tasks = [asyncio.create_task(owner.handle(item)) for item in sockets]
    while owner.pending_count < 4:
        await asyncio.sleep(0)
    fifth = Socket([])
    await owner.handle(fifth)
    assert fifth.closed == (1008, "FEATURE_DISABLED")
    tasks[0].cancel()
    await asyncio.gather(tasks[0], return_exceptions=True)
    replacement = Socket([hello()])
    replacement_task = asyncio.create_task(owner.handle(replacement))
    while not replacement.sent:
        await asyncio.sleep(0)
    assert replacement.sent[0]["kind"] == "CHALLENGE"
    replacement_task.cancel()
    for task in tasks[1:]:
        task.cancel()
    await asyncio.gather(replacement_task, *tasks[1:], return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_send", [0, 1])
async def test_blocked_challenge_or_ready_send_times_out_and_cleans_pending_role(blocked_send):
    selected = context()
    socket = Socket([hello(selected)])
    socket.block_send_at = blocked_send
    owner = transport()
    task = asyncio.create_task(owner.handle(socket))
    await socket.send_entered.wait()
    if blocked_send == 1:
        socket.release_send.set()
        while not socket.sent:
            await asyncio.sleep(0)
        challenge = socket.sent[0]
        proof = compute_proof(
            KEY,
            "PEER_PROOF",
            selected,
            b"p" * 32,
            b"h" * 32,
            challenge["challengeId"],
            NOW,
            datetime.fromisoformat(challenge["expiresAt"].replace("Z", "+00:00")),
        )
        socket.block_send_at = 1
        socket.release_send.clear()
        await socket.messages.put(
            json.dumps(
                {
                    "security": "luosandbox.control-auth.v1",
                    "kind": "PROOF",
                    "challengeId": challenge["challengeId"],
                    "peerProof": proof,
                }
            )
        )
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert owner.active_roles == () and owner.pending_count == 0


@pytest.mark.asyncio
async def test_monotonic_rollback_during_heartbeat_fails_closed_without_extending_lease():
    clock = Clock()
    selected = context()
    socket = Socket([hello(selected)])
    owner = transport(clock)
    task = asyncio.create_task(owner.handle(socket))
    while not socket.sent:
        await asyncio.sleep(0)
    challenge = socket.sent[0]
    proof = compute_proof(
        KEY,
        "PEER_PROOF",
        selected,
        b"p" * 32,
        b"h" * 32,
        challenge["challengeId"],
        NOW,
        datetime.fromisoformat(challenge["expiresAt"].replace("Z", "+00:00")),
    )
    await socket.messages.put(
        json.dumps(
            {
                "security": "luosandbox.control-auth.v1",
                "kind": "PROOF",
                "challengeId": challenge["challengeId"],
                "peerProof": proof,
            }
        )
    )
    while len(socket.sent) < 2:
        await asyncio.sleep(0)
    clock.value = 1.0
    await task
    assert owner.active_roles == ()
