"""CallResponseParser 深层次测试：换行边界、尾行 flush、语气兜底、取消丢弃。"""

from src.chat_session.call_response_parser import CallResponseParser


def test_flush_response_emits_tail_without_newline():
    parser = CallResponseParser("call-1")
    lines = parser.feed_text_delta("response-1", "[温柔]你好")
    assert lines == []  # 没有换行时不应立即产出
    lines = parser.flush_response("response-1")
    assert [line.content for line in lines] == ["你好"]
    assert lines[0].tone == "tender"
    assert lines[0].seq == 0


def test_flush_empty_response_returns_nothing():
    parser = CallResponseParser("call-1")
    parser.feed_text_delta("response-1", "")
    assert parser.flush_response("response-1") == []
    # 重复 flush 已删除的 buffer 不应报错
    assert parser.flush_response("response-1") == []


def test_cancel_response_discards_following_deltas_and_flush():
    parser = CallResponseParser("call-1")
    parser.feed_text_delta("response-1", "[中性]第一句\n")
    parser.cancel_response("response-1")
    assert parser.feed_text_delta("response-1", "[中性]第二句\n") == []
    assert parser.flush_response("response-1") == []
    # 取消后 seq 不再增长
    parser = CallResponseParser("call-1")
    parser.feed_text_delta("response-1", "[中性]一句\n")
    parser.cancel_response("response-1")
    parser.feed_text_delta("response-1", "[中性]二句\n")
    lines = parser.feed_text_delta("response-2", "[中性]新\n")
    lines += parser.flush_response("response-2")
    assert [line.seq for line in lines] == [0]


def test_empty_content_line_falls_back_to_default_utterance():
    parser = CallResponseParser("call-1")
    lines = parser.feed_text_delta("response-1", "[中性]   \n")
    assert len(lines) == 1
    assert lines[0].content == "嗯"
    assert lines[0].tone == "happy"


def test_tone_normalization_and_expression_mapping():
    parser = CallResponseParser("call-1")
    lines = parser.feed_text_delta("response-1", "[生气]走开\n[不存在]随便\n")
    assert [line.tone for line in lines] == ["angry", "happy"]
    assert [line.expression for line in lines] == ["生气脸", "微笑脸"]


def test_seq_and_audio_id_assigned_per_line_in_response_order():
    parser = CallResponseParser("call-1")
    lines = parser.feed_text_delta("response-1", "[中性]一\n[中性]二\n")
    assert [line.seq for line in lines] == [0, 1]
    assert all(line.call_id == "call-1" for line in lines)
    assert all(line.response_id == "response-1" for line in lines)
