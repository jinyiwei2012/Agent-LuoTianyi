"""电话播放 ACK 测试：completed/stopped 幂等、迟到 ACK、未 ACK 不落 assistant turn。"""

import asyncio
from datetime import datetime
from types import SimpleNamespace

from src.chat_session.call_models import CallState, CallTTSLine
from src.chat_session.call_stream import CallStream


class FakeWs:
    def __init__(self):
        self.sent = []

    async def send_json(self, data):
        self.sent.append(data)


class FakeWorker:
    def __init__(self):
        self.enqueued = []

    async def cancel_pending(self, **kwargs):
        pass

    async def enqueue(self, job):
        self.enqueued.append(job)

    def has_work(self, stream_id):
        return False


class FakeCallStore:
    def __init__(self):
        self.turns = []

    def append_turn(self, draft):
        self.turns.append(draft)
        return True


def make_stream() -> CallStream:
    ws_connection = SimpleNamespace(websocket=FakeWs(), user_uuid="user-1", user_name="user-1")
    return CallStream(
        call_id="call-1",
        user_id="user-1",
        user_name="user-1",
        character_id="luotianyi",
        ws_connection=ws_connection,
        config={"proactive_delay_seconds": 2, "playback_stop_ack_timeout_seconds": 3},
        realtime_dialogue_service=None,
        conversation_service=None,
        global_speaking_worker=FakeWorker(),
        agent_runtime=None,
        call_store=FakeCallStore(),
        llm_service=None,
        observability=None,
    )


def _line(response_id: str = "resp-1", seq: int = 0) -> CallTTSLine:
    return CallTTSLine(call_id="call-1", response_id=response_id, seq=seq, content="你好", tone="happy")


async def _enqueue(stream: CallStream, response_id: str = "resp-1") -> str:
    stream.state = CallState.ACTIVE
    await stream._enqueue_tts_line(_line(response_id))
    return stream.global_speaking_worker.enqueued[-1].job_content.audio_id


def test_playback_completed_writes_turn_once():
    stream = make_stream()
    audio_id = asyncio.run(_enqueue(stream))

    async def scenario():
        await stream._playback_completed({"audio_id": audio_id, "response_id": "resp-1"})
        await stream._playback_completed({"audio_id": audio_id, "response_id": "resp-1"})

    asyncio.run(scenario())
    assert len(stream.call_store.turns) == 1
    assert stream.call_store.turns[0].speaker == "assistant"
    assert stream.call_store.turns[0].text == "你好"
    if stream._proactive_task:
        stream._proactive_task.cancel()


def test_no_turn_without_playback_ack():
    stream = make_stream()
    asyncio.run(_enqueue(stream))
    assert stream.call_store.turns == []


def test_late_ack_after_line_cleanup_is_ignored():
    stream = make_stream()
    audio_id = asyncio.run(_enqueue(stream))

    async def scenario():
        await stream._playback_completed({"audio_id": audio_id, "response_id": "resp-1"})
        # 行已被消费并清理，迟到 ACK 不应重复落库
        await stream._playback_completed({"audio_id": audio_id, "response_id": "resp-1"})

    asyncio.run(scenario())
    assert len(stream.call_store.turns) == 1
    if stream._proactive_task:
        stream._proactive_task.cancel()


def test_playback_completed_mismatched_response_ignored():
    stream = make_stream()
    audio_id = asyncio.run(_enqueue(stream, response_id="resp-1"))

    async def scenario():
        await stream._playback_completed({"audio_id": audio_id, "response_id": "other"})

    asyncio.run(scenario())
    assert stream.call_store.turns == []


def test_playback_stopped_marks_cancelled_and_does_not_write_turn():
    stream = make_stream()
    audio_id = asyncio.run(_enqueue(stream))
    stream._interrupt_started_at["resp-1"] = 0.0

    async def scenario():
        await stream._playback_stopped({"audio_id": audio_id, "response_id": "resp-1"})

    asyncio.run(scenario())
    assert stream.call_store.turns == []  # 停止不代表完整播放
    assert stream._responses["resp-1"].cancelled is True
    assert "resp-1" not in stream._interrupt_started_at


def test_user_turn_written_on_transcription_completed():
    from src.utils.realtime_dialogue.models import RealtimeEvent, RealtimeEventType

    stream = make_stream()
    stream.state = CallState.ACTIVE

    async def scenario():
        await stream._handle_provider_event(
            RealtimeEvent(
                type=RealtimeEventType.INPUT_TRANSCRIPTION_COMPLETED,
                transcript="我想听歌",
                item_id="item-1",
            )
        )

    asyncio.run(scenario())
    assert len(stream.call_store.turns) == 1
    assert stream.call_store.turns[0].speaker == "user"
    assert stream.call_store.turns[0].text == "我想听歌"
