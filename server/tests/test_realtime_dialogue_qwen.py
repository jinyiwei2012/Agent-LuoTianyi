"""Qwen Realtime 适配器测试：事件归一化、上下文注入模式、工具结果、参数缓冲。"""

import asyncio
import json

from src.utils.realtime_dialogue.models import RealtimeEventType
from src.utils.realtime_dialogue.qwen_session import (
    QwenRealtimeSession,
    normalize_qwen_event,
    _with_model,
)


def test_with_model_appends_model_query_param():
    url = _with_model("wss://example.com/api-ws/v1/realtime", "qwen-audio-3.0-realtime-flash")
    assert "model=qwen-audio-3.0-realtime-flash" in url
    # 已存在的其他参数不被覆盖
    url = _with_model("wss://example.com/realtime?workspace=abc", "model-x")
    assert "workspace=abc" in url and "model=model-x" in url


def test_normalize_response_created_extracts_nested_id():
    event = normalize_qwen_event({"type": "response.created", "response": {"id": "resp-9"}})
    assert event.response_id == "resp-9"
    assert event.type is RealtimeEventType.RESPONSE_CREATED


def test_normalize_speech_events_keep_transcript_and_item():
    event = normalize_qwen_event(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "item-3",
            "transcript": "你好呀",
        }
    )
    assert event.item_id == "item-3"
    assert event.transcript == "你好呀"
    assert event.type is RealtimeEventType.INPUT_TRANSCRIPTION_COMPLETED


def test_normalize_usage_and_error_are_captured():
    usage_event = normalize_qwen_event(
        {"type": "response.done", "response": {"usage": {"input_tokens": 10}}}
    )
    assert usage_event.usage == {"input_tokens": 10}
    error_event = normalize_qwen_event({"type": "error", "error": {"message": "boom"}})
    assert error_event.error == {"message": "boom"}


def test_submit_tool_result_uses_function_call_output_item():
    class FakeWebSocket:
        def __init__(self):
            self.sent = []

        async def send(self, value):
            self.sent.append(json.loads(value))

    session = QwenRealtimeSession(
        config={"api_key": "key", "model": "m", "base_url": "wss://example"},
        trace_id="t",
        call_id="c",
        instructions="i",
        tools=[],
    )
    session.ws = FakeWebSocket()
    session._connected = True
    asyncio.run(session.submit_tool_result(call_id="fc-1", output="新增记忆2条"))
    payload = session.ws.sent[-1]
    assert payload["type"] == "conversation.item.create"
    assert payload["item"]["type"] == "function_call_output"
    assert payload["item"]["call_id"] == "fc-1"
    assert payload["item"]["output"] == "新增记忆2条"


def test_conversation_item_transport_mode_sends_create_and_delete():
    class FakeWebSocket:
        def __init__(self):
            self.sent = []

        async def send(self, value):
            self.sent.append(json.loads(value))

    session = QwenRealtimeSession(
        config={"api_key": "key", "model": "m", "base_url": "wss://example", "context_item_transport": "conversation_item"},
        trace_id="t",
        call_id="c",
        instructions="i",
        tools=[],
    )
    session.ws = FakeWebSocket()
    session._connected = True
    asyncio.run(session.append_context_item(role="system", text="记忆", item_id="m1"))
    assert session.ws.sent[-1]["type"] == "conversation.item.create"
    asyncio.run(session.delete_context_item("m1"))
    assert session.ws.sent[-1]["type"] == "conversation.item.delete"


def test_events_buffers_function_arguments_deltas():
    class FakeWs:
        def __init__(self, messages):
            self.messages = messages

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.messages:
                raise StopAsyncIteration
            return self.messages.pop(0)

    session = QwenRealtimeSession(
        config={"api_key": "key", "model": "m", "base_url": "wss://example"},
        trace_id="t",
        call_id="c",
        instructions="i",
        tools=[],
    )
    fake_ws = FakeWs(
        [
            r'''{"type": "response.function_call_arguments.delta", "call_id": "fc-1", "delta": "{\"quer"}''',
            r'''{"type": "response.function_call_arguments.delta", "call_id": "fc-1", "delta": "ies\":[\"a\"]}"}''',
            r'''{"type": "response.function_call_arguments.done", "call_id": "fc-1"}''',
        ]
    )
    session.ws = fake_ws
    session._connected = True

    async def collect():
        return [event async for event in session.events()]

    events = asyncio.run(collect())
    done = [event for event in events if event.type is RealtimeEventType.FUNCTION_ARGUMENTS_DONE]
    assert len(done) == 1
    assert json.loads(done[0].arguments) == {"queries": ["a"]}


def test_send_fails_when_session_not_connected():
    session = QwenRealtimeSession(
        config={"api_key": "key", "model": "m", "base_url": "wss://example"},
        trace_id="t",
        call_id="c",
        instructions="i",
        tools=[],
    )
    session._connected = False
    try:
        asyncio.run(session.append_audio("AAAA"))
        raise AssertionError("should raise when not connected")
    except RuntimeError as exc:
        assert "not connected" in str(exc)
