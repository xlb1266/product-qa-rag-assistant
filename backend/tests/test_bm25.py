"""BM25 单测：中文 bigram 分词与打分排序行为。"""

from app.core.bm25 import BM25Index, tokenize


def test_tokenize_cjk_bigrams():
    assert tokenize("空气小猪") == ["空气", "气小", "小猪"]


def test_tokenize_single_cjk_char_kept():
    assert tokenize("我") == ["我"]


def test_tokenize_english_words_lowercased():
    assert tokenize("AI app") == ["ai", "app"]


def test_tokenize_mixed_cjk_and_english():
    assert tokenize("用AI学外语") == ["用", "学外", "外语", "ai"]


def test_bm25_only_returns_matching_docs_sorted_desc():
    docs = [
        {"id": 1, "content": "空气小猪是语言学习工具"},
        {"id": 2, "content": "今天天气很好"},
        {"id": 3, "content": "语言学习需要坚持每天练习"},
    ]
    hits = BM25Index(docs).search("语言学习")
    ids = [cid for cid, _ in hits]
    assert set(ids) == {1, 3}
    scores = [s for _, s in hits]
    assert scores == sorted(scores, reverse=True)


def test_bm25_idf_favors_rare_term():
    docs = [
        {"id": 1, "content": "常见词 常见词 常见词"},
        {"id": 2, "content": "常见词 稀罕词"},
    ]
    hits = BM25Index(docs).search("稀罕词")
    assert hits[0][0] == 2


def test_bm25_top_n_limits_result_count():
    docs = [{"id": i, "content": f"搜索词 文档{i}"} for i in range(1, 6)]
    assert len(BM25Index(docs).search("搜索词", top_n=2)) == 2


def test_bm25_no_match_returns_empty():
    docs = [{"id": 1, "content": "语言学习"}]
    assert BM25Index(docs).search("完全无关的词") == []
