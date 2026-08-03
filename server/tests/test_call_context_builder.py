"""CallContextBuilder 测试：初始上下文组装、最近历史格式化、空历史兜底。"""

import asyncio
from types import SimpleNamespace

from src.chat_session.call_context_builder import CallContextBuilder


class FakeConscious:
    def __init__(self):
        self.description = "一位喜欢听歌的用户"
        self.preference_context = "喜欢温柔的语气"

    def load_user_expression_context(self, user_id):
        return SimpleNamespace(description=self.description, preference_context=self.preference_context)


class FakeCharacterRuntime:
    def __init__(self):
        self.conscious = FakeConscious()

    def dynamic_context(self):
        return {
            "character_persona": "洛天依：虚拟歌手，活泼可爱",
            "speaking_style": "口语化、温柔",
        }


class FakeAgentRuntime:
    def __init__(self):
        self.runtime = FakeCharacterRuntime()

    def get_character_runtime(self, character_id):
        return self.runtime


class FakeConversationService:
    def __init__(self, items):
        self.items = items
        self.last_kwargs = None

    async def get_recent_items(self, user_id, **kwargs):
        self.last_kwargs = kwargs
        return self.items


def make_builder(items):
    return CallContextBuilder(
        agent_runtime=FakeAgentRuntime(),
        conversation_service=FakeConversationService(items),
        config={},
    )


def test_build_assembles_system_prompt_with_character_and_user_context():
    builder = make_builder([])
    context = asyncio.run(builder.build(user_id="u1", character_id="luotianyi"))
    assert "洛天依人设：洛天依：虚拟歌手，活泼可爱" in context.system_prompt
    assert "洛天依表达偏好：口语化、温柔" in context.system_prompt
    assert "用户画像：一位喜欢听歌的用户" in context.system_prompt
    assert "用户偏好：喜欢温柔的语气" in context.system_prompt
    assert "search_memory" in context.system_prompt
    assert "一行一句" in context.system_prompt


def test_build_empty_history_falls_back():
    builder = make_builder([])
    context = asyncio.run(builder.build(user_id="u1", character_id="luotianyi"))
    assert context.recent_history_item == "无最近聊天记录。"
    assert context.start_request_item == "用户刚刚主动发起了语音电话。"


def test_history_formats_all_item_types():
    items = [
        {"type": "text", "content": "早上好", "source": "user", "meta_data": None},
        {"type": "text", "content": "早上好呀", "source": "agent", "meta_data": None},
        {"type": "sing", "content": "洛天依唱了《权御天下》", "source": "agent", "meta_data": None},
        {"type": "image", "content": "图片里有一只猫", "source": "user", "meta_data": {"terms": ["猫"]}},
        {"type": "call", "content": "语音通话120秒", "source": "agent", "meta_data": {"summary": "聊了喜欢的歌"}},
        {"type": "call", "content": "语音通话60秒", "source": "agent", "meta_data": None},
    ]
    builder = make_builder(items)
    context = asyncio.run(builder.build(user_id="u1", character_id="luotianyi"))
    lines = context.recent_history_item.splitlines()
    assert "[用户] 早上好" in lines
    assert "[洛天依] 早上好呀" in lines
    assert "[唱歌] 洛天依唱了《权御天下》" in lines
    assert "[图片描述] 图片里有一只猫" in lines
    assert "[语音通话] 语音通话120秒：聊了喜欢的歌" in lines
    assert "[语音通话] 语音通话60秒" in lines


def test_recent_items_uses_documented_window():
    builder = make_builder([])
    asyncio.run(builder.build(user_id="u1", character_id="luotianyi"))
    assert builder.conversation_service.last_kwargs["since_minutes"] == 10
    assert builder.conversation_service.last_kwargs["limit"] == 20
