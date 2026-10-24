import base64
import json
from dataclasses import dataclass

import pytest

from src.adapter.minecraft.postauth_frame import PostAuthFrame, TrustedFrameContext, encode_frame
from src.adapter.minecraft.postauth_keys import PostAuthSessionKeys
from src.adapter.minecraft.postauth_session import PostAuthHubSession, PostAuthSessionError, PostAuthSessionState
from tests.unit.adapter.test_minecraft_postauth_keys import CHALLENGE, PSK, transcript_data

CONNECTION = "00000000-0000-4000-8000-000000000002"


@dataclass
class Future:
    callback: object | None = None
    failed: bool = False
    cancelled_value: bool = False

    def add_done_callback(self, callback):
        self.callback = callback

    def cancelled(self):
        return self.cancelled_value

    def exception(self):
        return RuntimeError("failed") if self.failed else None

    def finish(self):
        self.callback(self)


def make(role="CLIENT_CONTROLLER", post_reject=False, abort_reject=False):
    now = [0]
    sent, posted, fenced, closed, observed = [], [], [], [], []
    auth = transcript_data(role)
    keys = PostAuthSessionKeys(auth, PSK)
    futures = []

    def send(raw):
        sent.append(raw)
        future = Future()
        futures.append(future)
        return future

    def post(callback):
        if post_reject:
            raise RuntimeError("post rejected")
        posted.append(callback)

    def owner_abort(callback):
        if abort_reject:
            raise RuntimeError("abort rejected")
        posted.append(callback)

    unknown = []
    session = PostAuthHubSession(
        authenticated=auth,
        keys=keys,
        connection_id=CONNECTION,
        monotonic_millis=lambda: now[0],
        send=send,
        post=post,
        owner_abort=owner_abort,
        report_cleanup_unknown=lambda: unknown.append(True),
        fence=lambda: fenced.append(True),
        close=lambda: closed.append(True),
        observe=observed.append,
    )
    return session, auth, keys, now, sent, posted, futures, fenced, closed, observed, unknown


def ack(auth, keys, sequence=0):
    context = TrustedFrameContext(authenticated=auth, connection_id=CONNECTION)
    return encode_frame(
        PostAuthFrame(
            connection_id=CONNECTION,
            challenge_id=CHALLENGE,
            sequence=sequence,
            message_kind="HEARTBEAT",
            route_id="",
            source_install_id="",
            source_connection_id="",
            recipient_connection_id="",
            challenge_expires_at="",
            payload=b"{}",
        ),
        context,
        keys,
        "PEER_TO_HUB",
    )


def notice(auth, keys, sequence):
    context = TrustedFrameContext(authenticated=auth, connection_id=CONNECTION)
    payload = json.dumps(
        {
            "security": "luosandbox.control-auth.v1",
            "kind": "BODY_BIND_NOTICE",
            "serverBootId": "00000000-0000-4000-8000-000000000003",
            "gameConnectionId": "00000000-0000-4000-8000-000000000004",
            "bodyId": "00000000-0000-4000-8000-000000000005",
            "gameNonce": base64.urlsafe_b64encode(b"n" * 32).decode().rstrip("="),
        },
        separators=(",", ":"),
    ).encode()
    return encode_frame(
        PostAuthFrame(
            connection_id=CONNECTION,
            challenge_id=CHALLENGE,
            sequence=sequence,
            message_kind="BODY_BIND_NOTICE",
            route_id="",
            source_install_id="",
            source_connection_id="",
            recipient_connection_id="",
            challenge_expires_at="",
            payload=payload,
        ),
        context,
        keys,
        "PEER_TO_HUB",
    )


def run_posted(posted):
    while posted:
        posted.pop(0)()


def test_open_ack_ready_heartbeat_and_timeout_boundaries():
    session, auth, keys, now, sent, posted, futures, fenced, closed, _, _ = make()
    session.start()
    assert session.state is PostAuthSessionState.WAIT_OPEN_ACK and len(sent) == 1
    now[0] = 4999
    session.tick()
    now[0] = 5000
    with pytest.raises(PostAuthSessionError):
        session.tick()
    assert fenced and closed and keys.verify("PEER_TO_HUB", b"x", b"x" * 32) is False
    session, auth, keys, now, sent, posted, futures, fenced, closed, _, _ = make()
    session.start()
    session.receive(ack(auth, keys))
    assert session.state is PostAuthSessionState.APPLICATION_READY
    futures[0].finish()
    run_posted(posted)
    now[0] = 500
    session.tick()
    assert len(sent) == 2
    now[0] = 2000
    with pytest.raises(PostAuthSessionError):
        session.tick()


def test_queue_is_eight_including_inflight_and_completion_is_ordered():
    session, auth, keys, now, sent, posted, futures, fenced, closed, _, _ = make()
    session.start()
    session.receive(ack(auth, keys))
    for value in range(7):
        now[0] = (value + 1) * 500
        session.receive(ack(auth, keys, value + 1))
        session.tick()
    assert len(sent) == 1
    now[0] = 4000
    with pytest.raises(PostAuthSessionError):
        session.tick()
    assert session.state is PostAuthSessionState.CLOSED
    futures[0].finish()
    run_posted(posted)
    assert len(sent) == 1


def test_invalid_ack_observer_failure_and_send_failure_fence_before_close():
    session, auth, keys, now, sent, posted, futures, fenced, closed, _, _ = make()
    session.start()
    with pytest.raises(PostAuthSessionError):
        session.receive(ack(auth, keys, 1))
    assert fenced and closed
    session, auth, keys, now, sent, posted, futures, fenced, closed, _, _ = make()
    session.start()
    futures[0].failed = True
    futures[0].finish()
    with pytest.raises(PostAuthSessionError):
        run_posted(posted)
    assert fenced and closed and session.state is PostAuthSessionState.CLOSED


def test_no_auto_activation_and_monotonic_rollback_fail_closed():
    session, auth, keys, now, sent, posted, futures, fenced, closed, observed, _ = make()
    session.start()
    assert not observed
    now[0] = -1
    with pytest.raises(PostAuthSessionError):
        session.tick()
    assert session.state is PostAuthSessionState.CLOSED


def test_business_frames_do_not_refresh_watchdog_and_late_heartbeat_cannot_revive():
    session, auth, keys, now, sent, posted, futures, fenced, closed, observed, _ = make()
    session.start()
    session.receive(ack(auth, keys))
    now[0] = 1999
    session.receive(notice(auth, keys, 1))
    assert observed
    now[0] = 2000
    with pytest.raises(PostAuthSessionError):
        session.receive(ack(auth, keys, 2))
    assert session.state is PostAuthSessionState.CLOSED and fenced and closed


def test_post_rejection_uses_owner_abort_and_abort_rejection_reports_unknown_only():
    session, auth, keys, now, sent, posted, futures, fenced, closed, observed, unknown = make(post_reject=True)
    session.start()
    futures[0].finish()
    assert session.state is PostAuthSessionState.WAIT_OPEN_ACK and not fenced
    run_posted(posted)
    assert session.state is PostAuthSessionState.CLOSED and fenced and closed and not unknown
    session, auth, keys, now, sent, posted, futures, fenced, closed, observed, unknown = make(
        post_reject=True, abort_reject=True
    )
    session.start()
    futures[0].finish()
    assert session.state is PostAuthSessionState.WAIT_OPEN_ACK and not fenced and unknown


def test_start_deadline_overflow_fences_before_external_send():
    session, auth, keys, now, sent, posted, futures, fenced, closed, observed, unknown = make()
    now[0] = (1 << 63) - 5000 + 1
    with pytest.raises(PostAuthSessionError):
        session.start()
    assert not sent and fenced and closed and session.state is PostAuthSessionState.CLOSED


def test_observer_reentrant_duplicate_is_reserved_before_callback_and_fails_closed_once():
    session, auth, keys, now, sent, posted, futures, fenced, closed, observed, unknown = make()
    duplicate = notice(auth, keys, 1)

    def observer(event):
        observed.append(event)
        with pytest.raises(PostAuthSessionError):
            session.receive(duplicate)

    session._observe = observer
    session.start()
    session.receive(ack(auth, keys))
    session.receive(duplicate)
    assert len(observed) == 1 and session.state is PostAuthSessionState.CLOSED
    assert len(fenced) == len(closed) == 1


def test_observer_close_and_exception_leave_no_post_callback_mutation():
    session, auth, keys, now, sent, posted, futures, fenced, closed, observed, unknown = make()
    session._observe = lambda event: session.close()
    session.start()
    session.receive(ack(auth, keys))
    session.receive(notice(auth, keys, 1))
    assert session.state is PostAuthSessionState.CLOSED and keys.verify("PEER_TO_HUB", b"x", b"x" * 32) is False
    session, auth, keys, now, sent, posted, futures, fenced, closed, observed, unknown = make()
    session._observe = lambda event: (_ for _ in ()).throw(RuntimeError("observer"))
    session.start()
    session.receive(ack(auth, keys))
    with pytest.raises(RuntimeError):
        session.receive(notice(auth, keys, 1))
    assert session.state is PostAuthSessionState.CLOSED and len(fenced) == len(closed) == 1


def test_cancelled_completion_fences_without_retrying_next_entry():
    session, auth, keys, now, sent, posted, futures, fenced, closed, observed, unknown = make()
    session.start()
    futures[0].cancelled_value = True
    futures[0].finish()
    with pytest.raises(PostAuthSessionError):
        run_posted(posted)
    assert session.state is PostAuthSessionState.CLOSED and len(sent) == 1 and len(fenced) == len(closed) == 1


def test_server_body_route_uses_ordered_queue_exact_metadata_and_payload():
    session, auth, keys, now, sent, posted, futures, fenced, closed, observed, unknown = make(role="SERVER_AUTHORITY")
    session.start()
    session.receive(ack(auth, keys))
    payload = notice(
        transcript_data("CLIENT_CONTROLLER"), PostAuthSessionKeys(transcript_data("CLIENT_CONTROLLER"), PSK), 1
    )
    raw_payload = json.loads(payload)["payload"]
    import base64

    decoded = base64.urlsafe_b64decode(raw_payload + "=" * (-len(raw_payload) % 4))
    session.send_body_bind_route(
        "60000000-0000-4000-8000-000000000006",
        "client-install",
        "70000000-0000-4000-8000-000000000007",
        decoded,
    )
    assert len(sent) == 1
    futures[0].finish()
    run_posted(posted)
    frame = json.loads(sent[1])
    assert frame["sequence"] == 1 and frame["messageKind"] == "BODY_BIND_ROUTE"
    assert frame["routeId"] == "60000000-0000-4000-8000-000000000006"
    assert frame["sourceInstallId"] == "client-install"
    assert frame["sourceConnectionId"] == "70000000-0000-4000-8000-000000000007"
    assert frame["recipientConnectionId"] == CONNECTION and frame["challengeExpiresAt"] == ""
    assert base64.urlsafe_b64decode(frame["payload"] + "=" * (-len(frame["payload"]) % 4)) == decoded


def test_body_route_wrong_role_and_precommit_failure_do_not_leave_sequence_or_queue():
    session, auth, keys, now, sent, posted, futures, fenced, closed, observed, unknown = make()
    session.start()
    session.receive(ack(auth, keys))
    with pytest.raises(PostAuthSessionError):
        session.send_body_bind_route("bad", "client", CONNECTION, b"{}")
    assert session.state is PostAuthSessionState.CLOSED and len(fenced) == len(closed) == 1
