"""CallStream 状态机测试：迁移约束、接通前挂断、重复 end 幂等、断线重连。"""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.chat_session.call_models import CallExitCode, CallState
from src.chat_session.call_stream import CallStream


class FakeWs:
    def __init__(self):
        self.sent = []

    async def send_json(self, data):
        self.sent.append(data)


class FakeWorker:
    def __init__(self):
        self.cancelled = []

    async def cancel_pending(self, **kwargs):
        self.cancelled.append(kwargs)

    async def enqueue(self, job):
        pass

    def has_work(self, stream_id):
        return False


class FakeCallStore:
    def __init__(self):
        self.preconnect_hangups = []
        self.settles = []

    def create_preconnect_hangup(self, **kwargs):
        self.preconnect_hangups.append(kwargs)
        return "conv-1"

    def settle_call_and_conversation(self, **kwargs):
        self.settles.append(kwargs)
        return "conv-1"

    def append_turn(self, draft):
        return True


def make_stream(state: CallState = CallState.ACTIVE, connected: bool = True) -> CallStream:
    ws_connection = SimpleNamespace(websocket=FakeWs(), user_uuid="user-1", user_name="user-1")
    stream = CallStream(
        call_id="call-1",
        user_id="user-1",
        user_name="user-1",
        character_id="luotianyi",
        ws_connection=ws_connection,
        config={"request_delay_seconds": 2, "reconnect_grace_seconds": 5, "proactive_delay_seconds": 2},
        realtime_dialogue_service=None,
        conversation_service=None,
        global_speaking_worker=FakeWorker(),
        agent_runtime=None,
        call_store=FakeCallStore(),
        llm_service=None,
        observability=None,
    )
    stream.state = state
    if connected:
        stream.connected_at = datetime.now()
    return stream


class EventRecorder:
    def __init__(self):
        self.events = []

    async def _send_event(self, event_type, payload):
        self.events.append((event_type, payload))
        return True


def test_invalid_state_transition_raises():
    stream = make_stream()
    stream.state = CallState.ENDED
    with pytest.raises(RuntimeError):
        stream._transition(CallState.ACTIVE, expected={CallState.REQUESTING})


def test_hangup_before_connected_writes_preconnect_record():
    stream = make_stream(connected=False)
    stream.state = CallState.REQUESTING
    stream.connected_at = None
    recorder = EventRecorder()
    stream._send_event = recorder._send_event

    async def scenario():
        await stream.hangup()

    asyncio.run(scenario())
    assert stream.state == CallState.ENDED
    assert stream.exit_code == int(CallExitCode.HANGUP_BEFORE_CONNECTED)
    assert len(stream.call_store.preconnect_hangups) == 1
    assert recorder.events[0][0].value == "call.ended"
    assert stream.call_store.settles == []


def test_end_is_idempotent_for_double_hangup():
    stream = make_stream()
    recorder = EventRecorder()
    stream._send_event = recorder._send_event

    async def scenario():
        await stream.end(CallExitCode.NORMAL, "user_hangup")
        await stream.end(CallExitCode.NORMAL, "user_hangup")

    asyncio.run(scenario())
    assert len(stream.call_store.settles) == 1
    ended = [e for e in recorder.events if e[0].value == "call.ended"]
    assert len(ended) == 1


def test_provider_failure_before_connect_sends_rejected_not_ended():
    stream = make_stream(connected=False)
    recorder = EventRecorder()
    stream._send_event = recorder._send_event

    async def scenario():
        await stream.end(CallExitCode.REALTIME_PROVIDER_FAILED, "connect refused")

    asyncio.run(scenario())
    types = [e[0].value for e in recorder.events]
    assert "call.rejected" in types
    assert "call.ended" not in types
    assert "call.error" not in types
    assert stream.call_store.preconnect_hangups == []
    assert stream.call_store.settles == []


def test_lost_connection_sends_reconnecting_and_is_reentrant():
    stream = make_stream()
    recorder = EventRecorder()
    stream._send_event = recorder._send_event

    async def scenario():
        await stream.lost_connection()
        first_deadline = stream._reconnect_deadline
        await stream.lost_connection()  # 重复断线不应抛错
        assert stream._reconnect_deadline >= first_deadline

    asyncio.run(scenario())
    assert stream.state == CallState.RECONNECTING
    assert recorder.events[0][0].value == "call.reconnecting"
    assert "reconnect_deadline" in recorder.events[0][1]


def test_reconnect_resumes_and_creates_provider_task():
    stream = make_stream()
    recorder = EventRecorder()
    stream._send_event = recorder._send_event

    class BlockingSession:
        """永远不产出事件的供应商会话，避免 provider reader 立刻触发 end()。"""

        async def events(self):
            while True:
                await asyncio.sleep(3600)
                yield

    stream.session = BlockingSession()

    async def scenario():
        await stream.lost_connection()
        new_ws = SimpleNamespace(websocket=FakeWs(), user_uuid="user-1", user_name="user-1")
        ok = await stream.reconnect(new_ws)
        assert ok
        return new_ws

    new_ws = asyncio.run(scenario())
    assert stream.state == CallState.ACTIVE
    assert stream.ws_connection is new_ws
    assert stream.provider_task is not None  # REQUESTING 断线后 resume 必须补建 reader
    assert any(e[0].value == "call.resumed" for e in recorder.events)
    stream.provider_task.cancel()


def test_reconnect_fails_when_state_not_reconnecting():
    stream = make_stream()
    asyncio.run(stream.lost_connection())
    # 先恢复成功一次
    asyncio.run(stream.reconnect(SimpleNamespace(websocket=FakeWs(), user_uuid="user-1", user_name="user-1")))
    # 状态已 ACTIVE，不能再次 resume
    async def scenario():
        return await stream.reconnect(SimpleNamespace(websocket=FakeWs(), user_uuid="user-1", user_name="user-1"))

    assert asyncio.run(scenario()) is False


def test_speech_started_cancels_response_and_sends_stop_playback():
    from src.utils.realtime_dialogue.models import RealtimeEvent, RealtimeEventType

    stream = make_stream()
    recorder = EventRecorder()
    stream._send_event = recorder._send_event
    stream._current_response_id = "resp-1"
    stream._responses["resp-1"] = type("R", (), {"pending_audio_ids": ["audio-1"], "cancelled": False, "completed_audio_ids": set()})()

    async def scenario():
        await stream._handle_speech_started(
            RealtimeEvent(type=RealtimeEventType.SPEECH_STARTED, event_id="evt-1")
        )

    asyncio.run(scenario())
    assert stream._responses["resp-1"].cancelled is True
    stop = [e for e in recorder.events if e[0].value == "call.stop_playback"]
    assert len(stop) == 1
    assert stop[0][1]["reason"] == "user_barge_in"
    # 等待 ACK 超时任务已注册
    assert "resp-1" in stream._playback_ack_tasks
    stream._playback_ack_tasks["resp-1"].cancel()
