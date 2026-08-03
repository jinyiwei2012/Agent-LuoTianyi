"""GlobalSpeakingWorker 调度测试：流内 FIFO、流间优先级、取消、队列上限。"""

import asyncio
import time
from types import SimpleNamespace

import pytest

from src.chat_session.call_models import CallTTSLine
from src.chat_session.dependency.global_speaking_worker import GlobalSpeakingWorker, SpeakingJob


@pytest.fixture(autouse=True)
def no_worker_loop(monkeypatch):
    """测试只驱动队列与调度器，不启动消费循环，避免 _run 与断言竞争。"""

    monkeypatch.setattr(GlobalSpeakingWorker, "start_if_needed", lambda self: None)


def _call_job(stream_id: str, seq: int, text: str = "你好呀", response_id: str | None = None, **kwargs) -> SpeakingJob:
    line = CallTTSLine(call_id="call-1", response_id=response_id or "resp-1", seq=seq, content=text, tone="happy")
    return SpeakingJob(
        send_reply_callback=kwargs.pop("send_reply_callback", lambda _: None),
        job_content=line,
        stream_id=stream_id,
        stream_seq=seq,
        response_id=response_id or "resp-1",
        cancellation_event=kwargs.pop("cancellation_event", asyncio.Event()),
        **kwargs,
    )


def test_stream_fifo_and_cross_stream_tie_break_by_enqueue_order():
    worker = GlobalSpeakingWorker({})

    async def scenario():
        for i in range(3):
            await worker.enqueue(_call_job("call:A", i))
        for i in range(2):
            await worker.enqueue(_call_job("call:B", i))
        picked = []
        for _ in range(5):
            job = await worker._take_next_job()
            picked.append((job.stream_id, job.stream_seq))
        return picked

    picked = asyncio.run(scenario())
    # 初始全部优先级 1，最早入队者先取；同流内严格 FIFO
    assert picked == [("call:A", 0), ("call:A", 1), ("call:A", 2), ("call:B", 0), ("call:B", 1)]


def test_starved_stream_gets_priority_boost():
    worker = GlobalSpeakingWorker({})

    async def scenario():
        await worker.enqueue(_call_job("call:hot", 0))
        # 短文本 → 估算时长很小（下限 0.3s）；长时间未被服务 → 优先级远大于 1
        await worker.enqueue(_call_job("call:stale", 0, text="好"))
        worker._last_started_at["call:stale"] = time.perf_counter() - 100
        first = await worker._take_next_job()
        return first

    first = asyncio.run(scenario())
    assert first.stream_id == "call:stale"  # 等待/估算比远大于 1，压制新流


def test_cancel_pending_removes_queued_jobs_and_sets_cancellation_events():
    worker = GlobalSpeakingWorker({})

    async def scenario():
        jobs = [_call_job("call:A", i, response_id="resp-1") for i in range(3)]
        for job in jobs:
            await worker.enqueue(job)
        removed = await worker.cancel_pending(stream_id="call:A", response_id="resp-1", reason="barge_in")
        assert removed == 3
        assert all(job.is_cancelled() for job in jobs)
        assert not worker.has_work("call:A")

    asyncio.run(scenario())


def test_cancel_pending_only_matching_response():
    worker = GlobalSpeakingWorker({})

    async def scenario():
        await worker.enqueue(_call_job("call:A", 0, response_id="resp-1"))
        await worker.enqueue(_call_job("call:A", 1, response_id="resp-2"))
        removed = await worker.cancel_pending(stream_id="call:A", response_id="resp-1", reason="barge_in")
        assert removed == 1
        assert worker.has_work("call:A")

    asyncio.run(scenario())


def test_queue_limit_raises_when_full():
    worker = GlobalSpeakingWorker({"max_total_jobs": 2, "max_stream_jobs": 2})

    async def scenario():
        await worker.enqueue(_call_job("call:A", 0))
        await worker.enqueue(_call_job("call:A", 1))
        with pytest.raises(RuntimeError):
            await worker.enqueue(_call_job("call:A", 2))

    asyncio.run(scenario())


def test_active_job_blocks_has_work_for_stream():
    worker = GlobalSpeakingWorker({})
    worker._active_job = _call_job("call:A", 0)
    assert worker.has_work("call:A")
    assert not worker.has_work("call:B")


def test_estimate_duration_for_call_line():
    job = _call_job("call:A", 0, text="一二三四五六七八九十")
    assert job.estimate_duration() >= 0.3
