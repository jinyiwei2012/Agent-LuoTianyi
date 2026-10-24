import base64
import json
from datetime import datetime, timezone

import pytest

from src.adapter.minecraft.control_auth import ControlAuthContext, PendingHandshakes, compute_proof
from src.adapter.minecraft.current_roles import AuthenticatedControlRoleOwner
from src.adapter.minecraft.postauth_frame import PostAuthFrame
from src.adapter.minecraft.v2_body_routes import V2BodyRouteError, V2BodyRouteOwner

NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)
SERVER_CONNECTION = "10000000-0000-4000-8000-000000000001"
CLIENT_CONNECTION = "20000000-0000-4000-8000-000000000002"
SERVER_BOOT = "30000000-0000-4000-8000-000000000003"
GAME_CONNECTION = "40000000-0000-4000-8000-000000000004"
BODY = "50000000-0000-4000-8000-000000000005"
ROUTES = [
    "60000000-0000-4000-8000-000000000006",
    "70000000-0000-4000-8000-000000000007",
    "80000000-0000-4000-8000-000000000008",
]


class Clock:
    def __init__(self):
        self.value = 0

    def __call__(self):
        return self.value


def _context(role, install):
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


def _handshake(context, connection, challenge):
    pending = PendingHandshakes()
    item = pending.begin(
        connection,
        context,
        b"p" * 32,
        NOW,
        hub_nonce=b"h" * 32,
        challenge_id=challenge,
        now=0,
        utc_now=NOW,
    )
    proof = compute_proof(
        b"k" * 32,
        "PEER_PROOF",
        context,
        item.peer_nonce,
        item.hub_nonce,
        item.challenge_id,
        item.issued_at,
        item.expires_at,
    )
    return pending.consume(
        connection,
        challenge,
        proof,
        b"k" * 32,
        expected_context=context,
        now=1,
        utc_now=NOW,
    )


def roles():
    server_context = _context("SERVER_AUTHORITY", "server")
    client_context = _context("CLIENT_CONTROLLER", "client")
    owner = AuthenticatedControlRoleOwner((server_context, client_context), "hub")
    leases = {}
    for role, context, connection, challenge in (
        ("SERVER_AUTHORITY", server_context, SERVER_CONNECTION, "90000000-0000-4000-8000-000000000009"),
        ("CLIENT_CONTROLLER", client_context, CLIENT_CONNECTION, "a0000000-0000-4000-8000-00000000000a"),
    ):
        reservation = owner.reserve(context, connection)
        owner.bind_challenge(reservation, challenge)
        leases[role] = owner.promote(reservation, _handshake(context, connection, challenge))
    return owner, leases


def notice(payload=None, **changes):
    raw = (
        payload
        or json.dumps(
            {
                "security": "luosandbox.control-auth.v1",
                "kind": "BODY_BIND_NOTICE",
                "serverBootId": SERVER_BOOT,
                "gameConnectionId": GAME_CONNECTION,
                "bodyId": BODY,
                "gameNonce": base64.urlsafe_b64encode(b"n" * 32).decode().rstrip("="),
            },
            separators=(",", ":"),
        ).encode()
    )
    values = dict(
        connection_id=CLIENT_CONNECTION,
        challenge_id="a0000000-0000-4000-8000-00000000000a",
        sequence=1,
        message_kind="BODY_BIND_NOTICE",
        route_id="",
        source_install_id="",
        source_connection_id="",
        recipient_connection_id="",
        challenge_expires_at="",
        payload=raw,
    )
    values.update(changes)
    return PostAuthFrame(**values)


def candidate(route_id, *, expires="2026-10-24T00:00:05.000Z", body=BODY, **changes):
    payload = json.dumps(
        {
            "security": "luosandbox.control-auth.v1",
            "kind": "BODY_BOUND",
            "serverBootId": SERVER_BOOT,
            "gameConnectionId": GAME_CONNECTION,
            "bodyId": body,
        },
        separators=(",", ":"),
    ).encode()
    values = dict(
        connection_id=SERVER_CONNECTION,
        challenge_id="90000000-0000-4000-8000-000000000009",
        sequence=1,
        message_kind="BODY_BOUND_CANDIDATE",
        route_id=route_id,
        source_install_id="",
        source_connection_id="",
        recipient_connection_id=SERVER_CONNECTION,
        challenge_expires_at=expires,
        payload=payload,
    )
    values.update(changes)
    return PostAuthFrame(**values)


@pytest.mark.asyncio
async def test_route_payload_metadata_candidate_and_original_ttl():
    owner, leases = roles()
    clock = Clock()
    route_ids = iter(ROUTES)
    routed = []
    routes = V2BodyRouteOwner(
        role_owner=owner,
        monotonic_millis=clock,
        utc_now=lambda: NOW,
        route_id=lambda: next(route_ids),
    )
    raw = notice().payload
    routes.route_notice(leases["CLIENT_CONTROLLER"], notice(raw), lambda *args: routed.append(args))
    assert routed == [(ROUTES[0], "client", CLIENT_CONNECTION, raw)]
    accepted = routes.accept_candidate(leases["SERVER_AUTHORITY"], candidate(ROUTES[0]))
    value = accepted.candidate
    assert value.route_id == ROUTES[0] and value.body_id == BODY
    assert routes.candidates() == (value,)
    clock.value = 4999
    routes.prune()
    assert routes.candidates() == (value,)
    clock.value = 5000
    routes.prune()
    assert routes.candidates() == () and routes._routes == {}


@pytest.mark.asyncio
async def test_capacity_includes_terminal_tombstones_and_collision_has_no_retry():
    owner, leases = roles()
    ids = iter((ROUTES[0], ROUTES[1], ROUTES[1]))
    routes = V2BodyRouteOwner(
        role_owner=owner,
        monotonic_millis=lambda: 0,
        utc_now=lambda: NOW,
        route_id=lambda: next(ids),
    )
    sent = []
    routes.route_notice(leases["CLIENT_CONTROLLER"], notice(), lambda *args: sent.append(args))
    routes._routes[ROUTES[0]].state = routes._routes[ROUTES[0]].state.INVALIDATED
    routes.route_notice(leases["CLIENT_CONTROLLER"], notice(), lambda *args: sent.append(args))
    with pytest.raises(V2BodyRouteError, match="capacity"):
        routes.route_notice(leases["CLIENT_CONTROLLER"], notice(), lambda *args: sent.append(args))
    assert len(routes._routes) == 2

    owner, leases = roles()
    duplicate = V2BodyRouteOwner(
        role_owner=owner,
        monotonic_millis=lambda: 0,
        utc_now=lambda: NOW,
        route_id=lambda: ROUTES[0],
    )
    duplicate.route_notice(leases["CLIENT_CONTROLLER"], notice(), lambda *args: None)
    with pytest.raises(V2BodyRouteError, match="identity"):
        duplicate.route_notice(leases["CLIENT_CONTROLLER"], notice(), lambda *args: None)


@pytest.mark.asyncio
async def test_candidate_mismatch_expiry_and_lease_invalidation_fail_closed():
    owner, leases = roles()
    routes = V2BodyRouteOwner(
        role_owner=owner,
        monotonic_millis=lambda: 0,
        utc_now=lambda: NOW,
        route_id=lambda: ROUTES[0],
    )
    routes.route_notice(leases["CLIENT_CONTROLLER"], notice(), lambda *args: None)
    with pytest.raises(V2BodyRouteError):
        routes.accept_candidate(leases["SERVER_AUTHORITY"], candidate(ROUTES[0], body=SERVER_BOOT))
    assert routes._routes[ROUTES[0]].state.value == "INVALIDATED"

    routes._routes.clear()
    routes.route_notice(leases["CLIENT_CONTROLLER"], notice(), lambda *args: None)
    with pytest.raises(V2BodyRouteError):
        routes.accept_candidate(
            leases["SERVER_AUTHORITY"],
            candidate(ROUTES[0], expires="2026-10-24T00:00:00.000Z"),
        )
    assert routes.candidates() == ()

    routes._routes.clear()
    routes.route_notice(leases["CLIENT_CONTROLLER"], notice(), lambda *args: None)
    owner.withdraw(leases["CLIENT_CONTROLLER"])
    routes.invalidate_lease(leases["CLIENT_CONTROLLER"])
    assert routes.candidates() == ()


@pytest.mark.asyncio
async def test_send_reentrancy_and_failure_publish_then_invalidate_without_controller_failure():
    owner, leases = roles()
    routes = V2BodyRouteOwner(
        role_owner=owner,
        monotonic_millis=lambda: 0,
        utc_now=lambda: NOW,
        route_id=lambda: ROUTES[0],
    )

    def reentrant(*_args):
        assert ROUTES[0] in routes._routes
        owner.withdraw(leases["SERVER_AUTHORITY"])
        routes.invalidate_lease(leases["SERVER_AUTHORITY"])

    routes.route_notice(leases["CLIENT_CONTROLLER"], notice(), reentrant)
    assert routes._routes[ROUTES[0]].state.value == "INVALIDATED"

    owner, leases = roles()
    routes = V2BodyRouteOwner(
        role_owner=owner,
        monotonic_millis=lambda: 0,
        utc_now=lambda: NOW,
        route_id=lambda: ROUTES[0],
    )
    routes.route_notice(
        leases["CLIENT_CONTROLLER"],
        notice(),
        lambda *_args: (_ for _ in ()).throw(RuntimeError("send failed")),
    )
    assert owner.is_current(leases["CLIENT_CONTROLLER"])
    assert routes._routes[ROUTES[0]].state.value == "INVALIDATED"


@pytest.mark.asyncio
async def test_clock_rollback_overflow_and_wrong_loop_precede_callbacks():
    owner, leases = roles()
    clock = Clock()
    calls = []
    routes = V2BodyRouteOwner(
        role_owner=owner,
        monotonic_millis=clock,
        utc_now=lambda: NOW,
        route_id=lambda: calls.append(True) or ROUTES[0],
    )
    clock.value = (1 << 63) - 4999
    with pytest.raises(V2BodyRouteError, match="overflow"):
        routes.route_notice(leases["CLIENT_CONTROLLER"], notice(), lambda *args: None)
    assert calls == []
    clock.value = 1
    with pytest.raises(V2BodyRouteError, match="clock"):
        routes.prune()

    errors = []

    def wrong_loop():
        import asyncio

        async def call():
            try:
                routes.invalidate_lease(leases["CLIENT_CONTROLLER"])
            except Exception as error:
                errors.append(error)

        asyncio.run(call())

    import threading

    thread = threading.Thread(target=wrong_loop)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive() and len(errors) == 1


@pytest.mark.asyncio
async def test_metadata_failures_precede_all_callbacks_and_state_changes():
    owner, leases = roles()
    calls = {"clock": 0, "utc": 0, "uuid": 0, "send": 0}

    def clock():
        calls["clock"] += 1
        return 0

    routes = V2BodyRouteOwner(
        role_owner=owner,
        monotonic_millis=clock,
        utc_now=lambda: calls.__setitem__("utc", calls["utc"] + 1) or NOW,
        route_id=lambda: calls.__setitem__("uuid", calls["uuid"] + 1) or ROUTES[0],
    )
    malformed = notice(recipient_connection_id=SERVER_CONNECTION)
    with pytest.raises(V2BodyRouteError, match="metadata"):
        routes.route_notice(
            leases["CLIENT_CONTROLLER"],
            malformed,
            lambda *args: calls.__setitem__("send", calls["send"] + 1),
        )
    assert calls == {"clock": 0, "utc": 0, "uuid": 0, "send": 0}
    assert routes._routes == {} and routes._generation == 0

    routes.route_notice(leases["CLIENT_CONTROLLER"], notice(), lambda *args: None)
    generation = routes._generation
    state = routes._routes[ROUTES[0]].state
    calls.update(clock=0, utc=0)
    for bad in (
        candidate(ROUTES[0], recipient_connection_id=CLIENT_CONNECTION),
        candidate(ROUTES[0], expires="2026-10-24T00:00:05.000000Z"),
        candidate(ROUTES[0], expires="2026-10-24 00:00:05.000Z"),
    ):
        with pytest.raises(V2BodyRouteError):
            routes.accept_candidate(leases["SERVER_AUTHORITY"], bad)
        assert routes._routes[ROUTES[0]].state is state
        assert routes._generation == generation and calls["clock"] == calls["utc"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ("replace", "fill", "collision", "net-same"))
async def test_uuid_generator_reentrancy_cannot_publish_stale_or_overwrite(mutation):
    owner, leases = roles()
    routes = None
    sent = []

    def generator():
        nonlocal routes
        if mutation == "replace":
            old = leases["SERVER_AUTHORITY"]
            owner.withdraw(old)
            context = old.handshake.context
            reservation = owner.reserve(context, old.connection_id)
            challenge = "b0000000-0000-4000-8000-00000000000b"
            owner.bind_challenge(reservation, challenge)
            leases["SERVER_AUTHORITY"] = owner.promote(reservation, _handshake(context, old.connection_id, challenge))
        elif mutation == "fill":
            for route_id in ROUTES[:2]:
                routes._routes[route_id] = object()
                routes._generation += 1
        elif mutation == "collision":
            routes._routes[ROUTES[0]] = object()
            routes._generation += 1
        else:
            routes._routes[ROUTES[1]] = object()
            routes._generation += 1
            routes._routes.pop(ROUTES[1])
            routes._generation += 1
        return ROUTES[0]

    routes = V2BodyRouteOwner(
        role_owner=owner,
        monotonic_millis=lambda: 0,
        utc_now=lambda: NOW,
        route_id=generator,
    )
    with pytest.raises(V2BodyRouteError, match="identity"):
        routes.route_notice(leases["CLIENT_CONTROLLER"], notice(), lambda *args: sent.append(args))
    assert sent == []
    if mutation in {"replace", "net-same"}:
        assert ROUTES[0] not in routes._routes
    if mutation == "fill":
        assert len(routes._routes) == 2


@pytest.mark.asyncio
async def test_stale_server_candidate_cannot_invalidate_replacement_record_with_same_route_id():
    owner, leases = roles()
    routes = V2BodyRouteOwner(
        role_owner=owner,
        monotonic_millis=lambda: 0,
        utc_now=lambda: NOW,
        route_id=lambda: ROUTES[0],
    )
    routes.route_notice(leases["CLIENT_CONTROLLER"], notice(), lambda *args: None)
    stale_server = leases["SERVER_AUTHORITY"]
    owner.withdraw(stale_server)
    context = stale_server.handshake.context
    reservation = owner.reserve(context, stale_server.connection_id)
    challenge = "b0000000-0000-4000-8000-00000000000b"
    owner.bind_challenge(reservation, challenge)
    replacement = owner.promote(reservation, _handshake(context, stale_server.connection_id, challenge))
    routes._routes[ROUTES[0]].server_lease = replacement
    generation = routes._generation
    with pytest.raises(V2BodyRouteError, match="origin"):
        routes.accept_candidate(stale_server, candidate(ROUTES[0]))
    assert routes._routes[ROUTES[0]].state.value == "ROUTED" and routes._generation == generation


@pytest.mark.asyncio
async def test_admission_receipt_is_exact_one_use_and_dto_never_authorizes():
    import copy

    owner, leases = roles()
    routes = V2BodyRouteOwner(
        role_owner=owner,
        monotonic_millis=lambda: 0,
        utc_now=lambda: NOW,
        route_id=lambda: ROUTES[0],
    )
    routes.route_notice(leases["CLIENT_CONTROLLER"], notice(), lambda *args: None)
    accepted = routes.accept_candidate(leases["SERVER_AUTHORITY"], candidate(ROUTES[0]))
    with pytest.raises(V2BodyRouteError):
        routes.inspect_for_preflight(copy.copy(accepted.receipt))
    context = routes.inspect_for_preflight(accepted.receipt)
    with pytest.raises(V2BodyRouteError):
        routes.inspect_for_preflight(accepted.receipt)
    token = routes._prevalidate_redemption(accepted.receipt, context)
    assert routes._redeem_prevalidated(accepted.receipt, token) is accepted.candidate
    with pytest.raises(V2BodyRouteError):
        routes._redeem_prevalidated(accepted.receipt, token)
    assert routes.candidates() == (accepted.candidate,)
