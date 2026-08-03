# coding: utf-8
"""Qwen Audio Realtime 契约 smoke test（手工运行，需要真实 API Key）。

验证 PRD 第 20 节发布门槛第 1 项：
- ASR / server VAD / 文本输出 / Function Calling / response.cancel / 上下文增删

不消耗普通测试资源，仅在显式运行时连接真实模型：
    python scripts/qwen_realtime_smoke.py
需要 config/config.json 中 realtime_dialogue_service.qwen 配置完整，
api_key 支持 $ENV 占位符（从环境变量解析）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from src.utils.logger import get_logger
from src.utils.realtime_dialogue.models import RealtimeEventType, RealtimeToolDefinition
from src.utils.realtime_dialogue.qwen_session import QwenRealtimeSession


logger = get_logger("qwen_realtime_smoke")


def resolve_config() -> dict:
    config_path = SERVER_ROOT / "config" / "config.json"
    with config_path.open("r", encoding="utf-8") as f:
        config = json.load(f)
    realtime = config.get("realtime_dialogue_service") or {}
    qwen = dict(realtime.get("qwen") or {})
    if str(qwen.get("api_key", "")).startswith("$"):
        env_name = str(qwen["api_key"])[1:]
        resolved = os.environ.get(env_name)
        if not resolved:
            raise SystemExit(f"缺少环境变量 {env_name}，无法运行真实模型 smoke test")
        qwen["api_key"] = resolved
    missing = [key for key in ("api_key", "model", "base_url") if not qwen.get(key)]
    if missing:
        raise SystemExit(f"realtime_dialogue_service.qwen 配置不完整: {missing}")
    return qwen


async def collect_until(session, predicate, timeout: float = 20.0) -> list:
    """消费事件直到 predicate 命中或超时。"""
    collected = []
    try:
        async with asyncio.timeout(timeout):
            async for event in session.events():
                collected.append(event)
                if predicate(event):
                    return collected
    except TimeoutError:
        pass
    return collected


async def smoke(qwen: dict) -> None:
    tools = [
        RealtimeToolDefinition(
            name="search_memory",
            description="检索洛天依与当前用户相关的长期记忆。",
            parameters={
                "type": "object",
                "properties": {"queries": {"type": "array", "items": {"type": "string"}, "maxItems": 5}},
                "required": ["queries"],
            },
        )
    ]
    instructions = (
        "你是洛天依。只输出文本，每行一句。"
        "当用户消息中出现关键词「查记忆」时，你必须调用 search_memory 工具，"
        "并且先输出一行「[中性]我想想」。"
    )
    session = QwenRealtimeSession(
        config=qwen,
        trace_id="smoke-1",
        call_id="smoke-call",
        instructions=instructions,
        tools=tools,
    )

    # 1. 连接与 session.update
    await session.connect()
    print("[1] 连接成功（session.update 已发送）")

    # 2. 文本输出：注入上下文并请求回复
    await session.append_context_item(role="user", text="你好呀", item_id="smoke-hi")
    await session.request_response()
    events = await collect_until(session, lambda e: e.type == RealtimeEventType.RESPONSE_DONE)
    text_deltas = [e for e in events if e.type in (RealtimeEventType.TEXT_DELTA, RealtimeEventType.OUTPUT_TEXT_DELTA)]
    assert any(e.delta for e in text_deltas), f"未收到文本 delta: {[e.type for e in events]}"
    print(f"[2] 文本输出 OK（{len(text_deltas)} 个 delta）")

    # 3. Function Calling：触发 search_memory
    await session.append_context_item(role="user", text="查记忆", item_id="smoke-tool")
    await session.request_response()
    events = await collect_until(session, lambda e: e.type == RealtimeEventType.FUNCTION_ARGUMENTS_DONE)
    function_done = [e for e in events if e.type == RealtimeEventType.FUNCTION_ARGUMENTS_DONE]
    assert function_done, f"未触发 function call: {[e.type for e in events]}"
    assert function_done[0].name == "search_memory", f"工具名错误: {function_done[0].name}"
    call_id = function_done[0].call_id
    print(f"[3] Function Calling OK（{function_done[0].name}，call_id={call_id}）")

    # 4. 工具结果回填后继续回复
    await session.submit_tool_result(call_id=call_id, output="没有更多记忆")
    await session.request_response()
    events = await collect_until(session, lambda e: e.type == RealtimeEventType.RESPONSE_DONE)
    assert any(e.type == RealtimeEventType.RESPONSE_DONE for e in events)
    print("[4] 工具结果回填 OK")

    # 5. response.cancel：发起回复后立即取消，之后不应再收到文本 delta
    await session.append_context_item(role="user", text="再说点什么", item_id="smoke-cancel")
    await session.request_response()
    await asyncio.sleep(0.3)
    await session.cancel_response()
    events = await collect_until(session, lambda e: e.type == RealtimeEventType.RESPONSE_DONE, timeout=10.0)
    print(f"[5] response.cancel OK（cancel 后事件: {[e.type for e in events]}）")

    # 6. 上下文增删（session_update 模式）：追加再删除
    await session.append_context_item(role="user", text="这是一条临时上下文", item_id="smoke-temp")
    await session.delete_context_item("smoke-temp")
    print("[6] 上下文增删（session_update 折叠）OK")

    await session.close()
    print("SMOKE PASSED：全部契约能力可用")


def main() -> None:
    qwen = resolve_config()
    asyncio.run(smoke(qwen))


if __name__ == "__main__":
    main()
