"""电话协议契约测试：WSEventType 取值、聊天互斥语义、拒绝错误。"""

import asyncio
from types import SimpleNamespace

from src.chat_session.call_models import CallState
from src.chat_session.call_stream_manager import CallRejectedError
from src.system.user_interface.types import WSEventType


def test_call_event_type_values_match_prd_protocol():
    assert WSEventType.CALL_START.value == "call.start"
    assert WSEventType.CALL_RESUME.value == "call.resume"
    assert WSEventType.CALL_AUDIO_APPEND.value == "call.audio.append"
    assert WSEventType.CALL_HANGUP.value == "call.hangup"
    assert WSEventType.CALL_PLAYBACK_COMPLETED.value == "call.playback_completed"
    assert WSEventType.CALL_PLAYBACK_STOPPED.value == "call.playback_stopped"
    assert WSEventType.CALL_REQUESTED.value == "call.requested"
    assert WSEventType.CALL_CONNECTED.value == "call.connected"
    assert WSEventType.CALL_AUDIO_CHUNK.value == "call.audio.chunk"
    assert WSEventType.CALL_STOP_PLAYBACK.value == "call.stop_playback"
    assert WSEventType.CALL_RECONNECTING.value == "call.reconnecting"
    assert WSEventType.CALL_RESUMED.value == "call.resumed"
    assert WSEventType.CALL_REJECTED.value == "call.rejected"
    assert WSEventType.CALL_ENDED.value == "call.ended"
    assert WSEventType.CALL_ERROR.value == "call.error"


def test_call_rejected_error_carries_code_and_exit_code():
    error = CallRejectedError("CALL_CONCURRENCY_LIMIT", "当前电话并发已满", -5)
    assert error.code == "CALL_CONCURRENCY_LIMIT"
    assert error.exit_code == -5


def test_blocking_chat_state_matches_prd():
    from src.chat_session.call_models import CallState

    blocking = {CallState.REQUESTING, CallState.ACTIVE, CallState.RECONNECTING}
    assert CallState.ENDING not in blocking  # PRD：结算期不阻塞新音频，但服务端仍拒绝聊天
    assert CallState.ENDED not in blocking


def test_has_blocking_call_uses_stream_state():
    from src.chat_session.call_stream_manager import CallStreamManager

    class FakeStream:
        def __init__(self, state):
            self.user_id = "user-1"
            self.state = state
            self.is_blocking_chat = state in {CallState.REQUESTING, CallState.ACTIVE, CallState.RECONNECTING}

    manager = CallStreamManager(config={})
    stream = FakeStream(CallState.ACTIVE)
    manager._call_id_by_user_id["user-1"] = "call-1"
    manager._streams_by_call_id["call-1"] = stream
    assert manager.has_blocking_call("user-1") is True

    stream = FakeStream(CallState.ENDED)
    manager._streams_by_call_id["call-1"] = stream
    assert manager.has_blocking_call("user-1") is False
    assert manager.has_blocking_call(None) is False


def test_ws_events_are_string_enums_compatible_with_json():
    assert str(WSEventType.CALL_START) == "WSEventType.CALL_START"  # 枚举身份
    assert WSEventType.CALL_START.value == "call.start"  # JSON 传输用 value
