"""CallSettlementCoordinator 测试：增量记忆写入、结束结算、摘要降级、状态回写。"""

import asyncio
from types import SimpleNamespace

from src.chat_session.call_models import CallExitCode
from src.chat_session.call_settlement import CallSettlementCoordinator


class FakeMind:
    def __init__(self):
        self.memory_writes = []
        self.event_memories = []
        self.profile_updates = []
        self.memory = self  # settlement 通过 mind.memory.write_event_memory 写入事件记忆

    async def write_topic_memories(self, **kwargs):
        self.memory_writes.append(kwargs)

    async def write_event_memory(self, **kwargs):
        self.event_memories.append(kwargs)

    async def update_user_profile_by_context(self, **kwargs):
        self.profile_updates.append(kwargs)


class FakeRuntime:
    def __init__(self, mind: FakeMind):
        self.mind = mind


class FakeAgentRuntime:
    def __init__(self, mind: FakeMind):
        self._runtime = FakeRuntime(mind)

    def get_character_runtime(self, character_id):
        return self._runtime


class FakeCallStore:
    def __init__(self):
        self.postprocess_state = None
        self.summary_updates = []
        self.status_updates = []

    def get_postprocess_state(self, call_id):
        return self.postprocess_state

    def update_summary(self, call_id, summary, status, error=None):
        self.summary_updates.append((call_id, summary, status, error))

    def update_postprocess_status(self, call_id, field, status, error=None):
        self.status_updates.append((call_id, field, status, error))


def _coordinator(mind: FakeMind) -> CallSettlementCoordinator:
    return CallSettlementCoordinator(
        config={},
        llm_service=None,
        call_store=FakeCallStore(),
        agent_runtime=FakeAgentRuntime(mind),
        character_id="luotianyi",
        observability=None,
    )


def test_write_memory_incremental_batches_by_ten():
    mind = FakeMind()
    coordinator = _coordinator(mind)
    turns = [{"speaker": "user" if i % 2 == 0 else "assistant", "text": f"行{i}"} for i in range(25)]
    asyncio.run(coordinator.write_memory_incremental(call_id="call-1", user_id="u1", turns=turns))
    # 非 final 只写到 20 行
    assert len(mind.memory_writes) == 2
    assert len(mind.memory_writes[0]["current_dialogue"].splitlines()) == 10
    asyncio.run(coordinator.write_memory_incremental(call_id="call-1", user_id="u1", turns=turns, final=True))
    assert len(mind.memory_writes) == 3
    assert len(mind.memory_writes[2]["current_dialogue"].splitlines()) == 5


def test_process_after_end_normal_call_writes_everything():
    mind = FakeMind()
    store = FakeCallStore()
    coordinator = CallSettlementCoordinator(
        config={},
        llm_service=None,
        call_store=store,
        agent_runtime=FakeAgentRuntime(mind),
        character_id="luotianyi",
        observability=None,
    )
    turns = [{"speaker": "user", "text": f"行{i}"} for i in range(3)]
    asyncio.run(
        coordinator.process_after_end(
            call_id="call-1", user_id="u1", exit_code=0, duration_seconds=120, turns=turns
        )
    )
    assert len(mind.memory_writes) == 1  # 尾部不足 10 行仍写一次
    assert len(mind.event_memories) == 1  # 正常结束写 event memory
    assert len(mind.profile_updates) == 1  # 正常结束更新画像
    assert ("call-1", "memory", "success") in [(s[0], s[1], s[2]) for s in store.status_updates]
    assert ("call-1", "profile", "success") in [(s[0], s[1], s[2]) for s in store.status_updates]
    # llm 未配置时使用确定性降级摘要
    summary = store.summary_updates[0]
    assert summary[2] == "success"
    assert summary[1]  # 非空摘要


def test_process_after_end_skips_profile_for_abnormal_exit():
    mind = FakeMind()
    store = FakeCallStore()
    coordinator = CallSettlementCoordinator(
        config={},
        llm_service=None,
        call_store=store,
        agent_runtime=FakeAgentRuntime(mind),
        character_id="luotianyi",
        observability=None,
    )
    turns = [{"speaker": "user", "text": "行0"}]
    asyncio.run(
        coordinator.process_after_end(
            call_id="call-1", user_id="u1", exit_code=-3, duration_seconds=10, turns=turns
        )
    )
    assert mind.event_memories == []  # 异常退出不写 event memory
    assert mind.profile_updates == []  # 异常退出不更新画像
    statuses = {(s[1], s[2]) for s in store.status_updates}
    assert ("profile", "skipped") in statuses


def test_process_after_end_is_idempotent_when_state_success():
    mind = FakeMind()
    store = FakeCallStore()
    store.postprocess_state = {
        "summary_status": "success",
        "memory_status": "success",
        "profile_status": "success",
        "summary": "已有摘要",
    }
    coordinator = CallSettlementCoordinator(
        config={},
        llm_service=None,
        call_store=store,
        agent_runtime=FakeAgentRuntime(mind),
        character_id="luotianyi",
        observability=None,
    )
    turns = [{"speaker": "user", "text": "行0"}]
    asyncio.run(
        coordinator.process_after_end(
            call_id="call-1", user_id="u1", exit_code=0, duration_seconds=10, turns=turns
        )
    )
    assert mind.memory_writes == []
    assert mind.event_memories == []
    assert mind.profile_updates == []
    assert store.summary_updates == []


def test_memory_write_failure_sets_memory_status_failed():
    class BrokenMind(FakeMind):
        async def write_topic_memories(self, **kwargs):
            raise RuntimeError("llm down")

    mind = BrokenMind()
    store = FakeCallStore()
    coordinator = CallSettlementCoordinator(
        config={},
        llm_service=None,
        call_store=store,
        agent_runtime=FakeAgentRuntime(mind),
        character_id="luotianyi",
        observability=None,
    )
    turns = [{"speaker": "user", "text": "行0"}]
    asyncio.run(
        coordinator.process_after_end(
            call_id="call-1", user_id="u1", exit_code=0, duration_seconds=10, turns=turns
        )
    )
    memory_status = [s for s in store.status_updates if s[1] == "memory"]
    assert memory_status and memory_status[0][2] == "failed"


def test_exit_code_contract():
    assert int(CallExitCode.TTS_FAILED) == -3
    assert int(CallExitCode.INTERNAL_ERROR) == -4
    assert int(CallExitCode.CONCURRENCY_REJECTED) == -5
