"""回答层单测：提示词拼装、JSON 容错解析、answerability 代码层拒答预判。"""

from types import SimpleNamespace

from app.core import answer as A
from app.core.answer import assess_answerability, build_answer_prompt
from app.core.retrieval import RetrievedChunk


def _chunk(score: float = 0.9, cid: int = 1) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=cid,
        document_id=1,
        document_title="测试文档",
        seq=1,
        heading_path="标题路径",
        content="知识正文",
        score=score,
    )


def _fake_runtime(threshold: float):
    return lambda: SimpleNamespace(get=lambda key: threshold)


# ---------- build_answer_prompt ----------


def test_prompt_fills_three_slots_in_order():
    prompt = build_answer_prompt(
        [_chunk()], [{"role": "user", "content": "历史消息"}], "最新问题"
    )
    assert "%s" not in prompt
    assert prompt.index("标题路径") < prompt.index("用户：历史消息") < prompt.index("最新问题")


def test_prompt_marks_empty_knowledge_and_history():
    prompt = build_answer_prompt([], [], "q")
    assert "（未检索到相关知识）" in prompt
    assert "（无，当前为首轮对话）" in prompt


# ---------- _extract_json ----------


def test_extract_json_from_fenced_block():
    assert A._extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_from_raw_text():
    assert A._extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_tolerates_surrounding_junk():
    assert A._extract_json('前面有废话 {"a": 1} 后面也有') == {"a": 1}


# ---------- assess_answerability ----------


def test_refuse_when_retrieval_empty():
    ok, reason = assess_answerability([], rerank_used=True)
    assert ok is False
    assert "为空" in reason


def test_refuse_when_top_score_below_threshold(monkeypatch):
    monkeypatch.setattr(A, "get_runtime", _fake_runtime(0.6))
    ok, reason = assess_answerability([_chunk(score=0.55)], rerank_used=True)
    assert ok is False
    assert "低于拒答阈值" in reason


def test_pass_when_top_score_at_threshold(monkeypatch):
    monkeypatch.setattr(A, "get_runtime", _fake_runtime(0.6))
    ok, reason = assess_answerability([_chunk(score=0.6)], rerank_used=True)
    assert ok is True
    assert reason == ""


def test_skip_score_check_when_rerank_degraded(monkeypatch):
    monkeypatch.setattr(A, "get_runtime", _fake_runtime(0.6))
    ok, reason = assess_answerability([_chunk(score=0.1)], rerank_used=False)
    assert ok is True
    assert reason == ""
