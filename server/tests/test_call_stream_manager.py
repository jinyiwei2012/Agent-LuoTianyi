"""CallStreamManager 测试：5 路并发、第 6 路拒绝、用户互斥、重连、超时清理。"""

import asyncio
import time
from types import SimpleNamespace

import pytest

from src.chat_session import call_stream_manager as csm_module
from src.chat_session.call_models import CallExitCode, CallState
from src.chat_session.call_stream_manager import CallRejectedError, CallStreamManager


class FakeStream:
    """最小 CallStream 替身，只维护 manager 需要观察的状态。"""

    def __init__(self, **kwargs):
        self.call_id = kwargs["call_id"]
        self.user_id = kwargs["user_id"]
        self.user_name = kwargs.get("user_name", "")
        self.character_id = kwargs.get("character_id", "luotianyi")
        self.ws_connection = kwargs["ws_connection"]
        self.state = kwargs.get("state", CallState.REQUESTING)
        self.started = False
        self.ended_calls = []
        self.reconnect_ok = kwargs.get("reconnect_ok", True)
        self._reconnect_deadline = None

    @property
    def is_blocking_chat(self):
        return self.state in {CallState.REQUESTING, CallState.ACTIVE, CallState.RECONNECTING}

    async def start(self):
        self.started = True

    async def reconnect(self, ws_connection):
        self.ws_connection = ws_connection
        if not self.reconnect_ok:
            return False
        self.state = CallState.ACTIVE
        return True

    async def lost_connection(self):
        if self.state in {CallState.ENDING, CallState.ENDED}:
            return
        self.state = CallState.RECONNECTING

    async def end(self, exit_code, reason):
        self.ended_calls.append((int(exit_code), reason))
        self.state = CallState.ENDED


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setattr(csm_module, "CallStream", FakeStream)
    return CallStreamManager(
        config={"max_concurrent_calls": 5, "reconnect_grace_seconds": 5},
        conversation_service=object(),
        global_speaking_worker=object(),
        realtime_dialogue_service=object(),
        agent_runtime=object(),
        call_store=object(),
    )


def _ws(user_uuid: str):
    return SimpleNamespace(user_uuid=user_uuid, user_name=user_uuid)


def test_five_concurrent_calls_allowed_and_sixth_rejected(manager):
    async def scenario():
        for i in range(5):
            stream = await manager.start_call(ws_connection=_ws(f"user-{i}"))
            assert stream.state is not CallState.ENDED
        with pytest.raises(CallRejectedError) as exc_info:
            await manager.start_call(ws_connection=_ws("user-6"))
        assert exc_info.value.code == "CALL_CONCURRENCY_LIMIT"
        assert exc_info.value.exit_code == int(CallExitCode.CONCURRENCY_REJECTED)

    asyncio.run(scenario())


def test_same_user_second_call_rejected_with_call_in_progress(manager):
    async def scenario():
        await manager.start_call(ws_connection=_ws("user-1"))
        with pytest.raises(CallRejectedError) as exc_info:
            await manager.start_call(ws_connection=_ws("user-1"))
        assert exc_info.value.code == "CALL_IN_PROGRESS"

    asyncio.run(scenario())


def test_same_client_request_id_returns_existing_stream(manager):
    async def scenario():
        first = await manager.start_call(ws_connection=_ws("user-1"), client_request_id="req-1")
        second = await manager.start_call(ws_connection=_ws("user-1"), client_request_id="req-1")
        assert second is first

    asyncio.run(scenario())


def test_resume_call_ownership_and_resumability(manager):
    async def scenario():
        stream = await manager.start_call(ws_connection=_ws("user-1"))
        stream.state = CallState.RECONNECTING
        with pytest.raises(CallRejectedError) as exc_info:
            await manager.resume_call(ws_connection=_ws("intruder"), call_id=stream.call_id)
        assert exc_info.value.code == "CALL_NOT_FOUND"

        resumed = await manager.resume_call(ws_connection=_ws("user-1"), call_id=stream.call_id)
        assert resumed is stream

    asyncio.run(scenario())


def test_resume_call_not_resumable_after_deadline(manager):
    async def scenario():
        stream = await manager.start_call(ws_connection=_ws("user-1"))
        stream.state = CallState.RECONNECTING
        stream.reconnect_ok = False
        with pytest.raises(CallRejectedError) as exc_info:
            await manager.resume_call(ws_connection=_ws("user-1"), call_id=stream.call_id)
        assert exc_info.value.exit_code == int(CallExitCode.RECONNECT_TIMEOUT)

    asyncio.run(scenario())


def test_has_blocking_call_reflects_stream_state(manager):
    async def scenario():
        stream = await manager.start_call(ws_connection=_ws("user-1"))
        stream.state = CallState.ACTIVE
        assert manager.has_blocking_call("user-1")
        stream.state = CallState.ENDED
        assert not manager.has_blocking_call("user-1")

    asyncio.run(scenario())


def test_cleanup_expired_reconnecting_streams(manager):
    async def scenario():
        stream = await manager.start_call(ws_connection=_ws("user-1"))
        stream.state = CallState.RECONNECTING
        stream._reconnect_deadline = time.monotonic() - 1
        await manager.cleanup_expired_streams()
        assert stream.state == CallState.ENDED
        assert stream.ended_calls[0][0] == int(CallExitCode.RECONNECT_TIMEOUT)
        assert manager.get_by_user_id("user-1") is None

    asyncio.run(scenario())


def test_release_clears_registries(manager):
    async def scenario():
        stream = await manager.start_call(ws_connection=_ws("user-1"))
        await manager.release(stream.call_id)
        assert manager.get_by_call_id(stream.call_id) is None
        assert manager.get_by_user_id("user-1") is None

    asyncio.run(scenario())


def test_stop_background_services_ends_active_streams(manager):
    async def scenario():
        stream = await manager.start_call(ws_connection=_ws("user-1"))
        stream.state = CallState.ACTIVE
        await manager.stop_background_services()
        assert stream.state == CallState.ENDED
        assert manager._streams_by_call_id == {}
        assert manager._call_id_by_user_id == {}

    asyncio.run(scenario())
