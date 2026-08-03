"""CallMemoryPool 深层次测试：去重、淘汰、批量超限、删除失败。"""

import asyncio
from types import SimpleNamespace

import pytest

from src.chat_session.call_memory_pool import CallMemoryPool


class FakeSession:
    def __init__(self):
        self.created = []
        self.deleted = []
        self.fail_delete = False

    async def append_context_item(self, **kwargs):
        self.created.append(kwargs)

    async def delete_context_item(self, item_id):
        if self.fail_delete:
            raise RuntimeError("delete failed")
        self.deleted.append(item_id)


def _hit(memory_id: str, text: str = "记忆内容"):
    return SimpleNamespace(record=SimpleNamespace(id=memory_id), rendered_text=text)


def test_dedupe_across_batches_and_within_batch():
    session = FakeSession()
    pool = CallMemoryPool(session=session, limit=10)
    first = asyncio.run(pool.add_hits([_hit("m1"), _hit("m1"), _hit("m2")]))
    assert first.added_count == 2  # 批内去重
    second = asyncio.run(pool.add_hits([_hit("m1"), _hit("m3")]))
    assert second.added_count == 1  # 已注入的 memory_id 不重复注入
    assert pool.memory_ids == ("m1", "m2", "m3")
    assert len(session.created) == 3


def test_evicts_oldest_after_batch_within_limit():
    session = FakeSession()
    pool = CallMemoryPool(session=session, limit=2)
    asyncio.run(pool.add_hits([_hit("m1"), _hit("m2")]))
    result = asyncio.run(pool.add_hits([_hit("m3")]))
    assert result.deleted_count == 1
    assert pool.memory_ids == ("m2", "m3")
    assert session.deleted == ["call-memory-m1"]


def test_batch_exceeding_limit_keeps_entire_batch():
    session = FakeSession()
    pool = CallMemoryPool(session=session, limit=2)
    asyncio.run(pool.add_hits([_hit("m1"), _hit("m2")]))
    result = asyncio.run(pool.add_hits([_hit("m3"), _hit("m4"), _hit("m5")]))
    assert result.deleted_count == 2  # 旧池全删
    assert pool.memory_ids == ("m3", "m4", "m5")  # 本轮批次完整保留
    assert set(session.deleted) == {"call-memory-m1", "call-memory-m2"}


def test_empty_or_all_duplicate_hits_return_no_more_memory():
    session = FakeSession()
    pool = CallMemoryPool(session=session, limit=10)
    assert asyncio.run(pool.add_hits([])).status == "no_more_memory"
    asyncio.run(pool.add_hits([_hit("m1")]))
    assert asyncio.run(pool.add_hits([_hit("m1")])).status == "no_more_memory"
    # 空文本的命中同样被忽略
    assert asyncio.run(pool.add_hits([_hit("m2", "")])).status == "no_more_memory"


def test_delete_failure_propagates_to_caller():
    session = FakeSession()
    pool = CallMemoryPool(session=session, limit=2)
    asyncio.run(pool.add_hits([_hit("m1"), _hit("m2")]))
    session.fail_delete = True
    with pytest.raises(RuntimeError):
        asyncio.run(pool.add_hits([_hit("m3")]))


def test_custom_id_factory_and_clear():
    session = FakeSession()
    pool = CallMemoryPool(session=session, limit=10, id_factory=lambda mid: f"pool-{mid}")
    asyncio.run(pool.add_hits([_hit("m1")]))
    assert session.created[-1]["item_id"] == "pool-m1"
    asyncio.run(pool.clear())
    assert session.deleted == ["pool-m1"]
    assert pool.memory_ids == ()
