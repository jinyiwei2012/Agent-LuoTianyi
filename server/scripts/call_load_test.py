# coding: utf-8
"""电话调度压测脚本：用 fake TTS 驱动 GlobalSpeakingWorker 的 5 电话流 + 1 聊天流。

用于验收 PRD 第 18.2 节：
- GPT-SoVITS 实际并发始终为 1（这里用 fake 模拟，观测同一时刻活跃生成器数量）
- 每流任务严格 FIFO
- 普通聊天 TTS 排队时间（enqueue → 首个包开始处理）
- 取消任务生效

不依赖真实 GPT-SoVITS / Qwen，可离线运行：
    python scripts/call_load_test.py [--calls 5] [--jobs-per-call 20] [--chat-jobs 10]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Generator

SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from src.chat_session.call_models import CallTTSLine
from src.chat_session.dependency.global_speaking_worker import GlobalSpeakingWorker, SpeakingJob


class FakeSpeech:
    """fake TTS：每个句子产出若干音频分片，模拟合成耗时；统计活跃生成器并发峰值。"""

    def __init__(self) -> None:
        self._active = 0
        self._peak = 0
        self._lock = threading.Lock()

    def say_stream(self, character_id: str, text: str, tone: str) -> Generator:
        with self._lock:
            self._active += 1
            self._peak = max(self._peak, self._active)
        try:
            chunk_count = max(1, len(text) // 4)
            for _ in range(chunk_count):
                time.sleep(0.01)  # 模拟单包合成耗时
                yield "QUJD"  # base64 音频
        finally:
            with self._lock:
                self._active -= 1

    @property
    def peak_concurrency(self) -> int:
        with self._lock:
            return self._peak


class FakeCapabilities:
    def __init__(self, speech: FakeSpeech) -> None:
        self.speech = speech


def make_call_line(call_id: str, seq: int, text: str) -> CallTTSLine:
    return CallTTSLine(call_id=call_id, response_id=f"resp-{call_id}", seq=seq, content=text, tone="happy")


async def run_load_test(calls: int, jobs_per_call: int, chat_jobs: int) -> dict[str, Any]:
    speech = FakeSpeech()
    worker = GlobalSpeakingWorker({})
    worker.set_capabilities(FakeCapabilities(speech))

    done_order: dict[str, list[int]] = defaultdict(list)
    started_at: dict[str, float] = {}
    chat_first_wait: float | None = None

    async def make_recorded_job(stream_id: str, seq: int, text: str) -> SpeakingJob:
        line = make_call_line(stream_id, seq, text)
        job = SpeakingJob(
            send_reply_callback=lambda _r: None,  # 下面会被替换
            job_content=line,
            character_id="luotianyi",
            stream_id=stream_id,
            stream_seq=seq,
            response_id=line.response_id,
            cancellation_event=asyncio.Event(),
        )
        stream = stream_id
        order = seq

        async def wrapped(response) -> None:
            nonlocal chat_first_wait
            if stream not in started_at:
                started_at[stream] = time.perf_counter()
            if response.is_final_package:
                done_order[stream].append(order)
                if stream.startswith("chat:") and len(done_order[stream]) == 1:
                    chat_first_wait = time.perf_counter() - started_at[stream]

        job.send_reply_callback = wrapped  # type: ignore[assignment]
        return job

    # 灌入任务：聊天流先入队（估算时长更长），验证优先级不会饿死电话流
    for seq in range(chat_jobs):
        job = await make_recorded_job("chat:u1", seq, "很长的聊天句子" * 30)
        await worker.enqueue(job)
    for call_index in range(calls):
        for seq in range(jobs_per_call):
            job = await make_recorded_job(f"call:{call_index}", seq, f"电话句子{seq}" * 3)
            await worker.enqueue(job)

    # 取消 call:0 流的全部任务（模拟打断）
    removed = await worker.cancel_pending(stream_id="call:0", response_id="resp-call:0", reason="load_test_cancel")

    # enqueue 已通过 start_if_needed 启动唯一消费循环，这里只等待完成
    total_jobs = chat_jobs + calls * jobs_per_call
    deadline = time.time() + 60
    while time.time() < deadline:
        if sum(len(v) for v in done_order.values()) >= total_jobs - removed:
            break
        await asyncio.sleep(0.05)
    await worker.stop()

    streams = {
        stream_id: {"done_count": len(v), "fifo_ok": (v == sorted(v))}
        for stream_id, v in done_order.items()
    }
    fifo_ok_all = all(info["fifo_ok"] for info in streams.values())
    return {
        "peak_tts_concurrency": speech.peak_concurrency,
        "streams": streams,
        "fifo_ok_all": fifo_ok_all,
        "cancelled_jobs": removed,
        "chat_first_job_queue_wait_seconds": round(chat_first_wait, 3) if chat_first_wait is not None else None,
        "total_done": sum(len(v) for v in done_order.values()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="电话 TTS 调度压测（fake 驱动，无需真实 TTS）")
    parser.add_argument("--calls", type=int, default=5)
    parser.add_argument("--jobs-per-call", type=int, default=20)
    parser.add_argument("--chat-jobs", type=int, default=10)
    args = parser.parse_args()

    result = asyncio.run(run_load_test(args.calls, args.jobs_per_call, args.chat_jobs))
    print(json.dumps(result, ensure_ascii=False, indent=2))

    # 断言（对齐 PRD 18.2）
    assert result["peak_tts_concurrency"] == 1, "TTS 并发必须为 1"
    assert result["fifo_ok_all"], "流内任务顺序必须严格 FIFO"
    assert result["cancelled_jobs"] >= 1, "取消任务应生效"
    assert result["total_done"] == args.chat_jobs + args.calls * args.jobs_per_call - result["cancelled_jobs"]
    print("压测断言通过：并发=1、流内 FIFO、取消生效、任务数一致")


if __name__ == "__main__":
    main()
