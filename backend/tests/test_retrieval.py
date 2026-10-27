"""混合检索编排单测：分数归一化、RRF 融合数学、重排阈值过滤、降级与 mock 路径。

外部依赖（embedding / FAISS / BM25 / rerank / DB）全部用假对象替换，
不发任何网络请求、不建任何数据库。
"""

import asyncio
from typing import Any

import pytest

from app.core import retrieval as R
from app.core.reranker import RerankError

BASE_VALUES: dict[str, Any] = {
    "retrieval.semantic_top_n": 10,
    "retrieval.candidate_top_n": 8,
    "retrieval.rerank_top_k": 4,
    "retrieval.rerank_threshold": 0.6,
    "debug.mock_third_party": False,
}

ROWS = [
    {"id": i, "document_id": 1, "seq": i, "heading_path": f"H{i}",
     "content": f"C{i}", "document_title": "T"}
    for i in (1, 2, 3)
]


class FakeRuntime:
    def __init__(self, values: dict[str, Any]) -> None:
        self._v = values

    def get(self, key: str) -> Any:
        return self._v[key]


class FakeEmbeddingClient:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class FakeVectorStore:
    def __init__(self, hits: list[tuple[int, float]]) -> None:
        self._hits = hits

    def search(self, vector: list[float], k: int = 5) -> list[tuple[int, float]]:
        return self._hits[:k]


class FakeBM25:
    def __init__(self, hits: list[tuple[int, float]]) -> None:
        self._hits = hits

    def search(self, query: str, top_n: int = 10) -> list[tuple[int, float]]:
        return self._hits[:top_n]


class FakeReranker:
    def __init__(self, pairs: list[tuple[int, float]] | None = None,
                 error: bool = False) -> None:
        self._pairs = pairs or []
        self._error = error

    async def rerank(self, query: str, documents: list[str],
                     top_n: int | None = None) -> list[tuple[int, float]]:
        if self._error:
            raise RerankError("模拟重排失败")
        return self._pairs


class FakeDB:
    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        return [r for r in ROWS if r["id"] in params]


def _patch(monkeypatch, semantic, bm25, reranker, values=None):
    v = values if values is not None else BASE_VALUES
    monkeypatch.setattr(R, "get_runtime", lambda: FakeRuntime(v))
    monkeypatch.setattr(R, "get_embedding_client", lambda: FakeEmbeddingClient())
    monkeypatch.setattr(R, "get_vector_store", lambda: FakeVectorStore(semantic))
    monkeypatch.setattr(R, "get_bm25_index", lambda: FakeBM25(bm25))
    monkeypatch.setattr(R, "get_reranker", lambda: reranker)
    monkeypatch.setattr(R, "get_db", lambda: FakeDB())


def _run(coro):
    return asyncio.run(coro)


def test_score_normalization_and_fusion_order(monkeypatch):
    # 语义余弦 1.0 -> 1.0、0.0 -> 0.5；BM25 按最大值归一化 10 -> 1.0、5 -> 0.5
    _patch(
        monkeypatch,
        semantic=[(1, 1.0), (2, 0.0)],
        bm25=[(2, 10.0), (3, 5.0)],
        reranker=FakeReranker(pairs=[(0, 0.9), (1, 0.55), (2, 0.7)]),
    )
    result = _run(R.retrieve("q"))

    assert [(h.chunk_id, h.score) for h in result.semantic_hits] == [(1, 1.0), (2, 0.5)]
    assert [(h.chunk_id, h.score) for h in result.bm25_hits] == [(2, 1.0), (3, 0.5)]

    # chunk2 两路命中：rrf = 1/(k+2) + 1/(k+1)，除以理论最大值 2/(k+1)
    k = R.RRF_K
    expected_2 = (1 / (k + 2) + 1 / (k + 1)) / (2 / (k + 1))
    expected_1 = (1 / (k + 1)) / (2 / (k + 1))
    fused = {h.chunk_id: h.score for h in result.fused_hits}
    assert [h.chunk_id for h in result.fused_hits] == [2, 1, 3]
    assert fused[2] == pytest.approx(expected_2, abs=1e-9)
    assert fused[1] == pytest.approx(expected_1, abs=1e-9)


def test_rerank_threshold_filters_low_score(monkeypatch):
    _patch(
        monkeypatch,
        semantic=[(1, 1.0), (2, 0.0)],
        bm25=[(2, 10.0), (3, 5.0)],
        reranker=FakeReranker(pairs=[(0, 0.9), (1, 0.55), (2, 0.7)]),
    )
    result = _run(R.retrieve("q"))

    # 候选 [2,1,3]：0.55 < 阈值 0.6 被过滤，保留 [2, 3] 按分数降序
    assert result.rerank_used is True
    assert [c.chunk_id for c in result.chunks] == [2, 3]
    assert result.chunks[0].score == 0.9
    top = result.chunks[0]
    assert (top.semantic_score, top.bm25_score) == (0.5, 1.0)


def test_rerank_failure_degrades_to_rrf_order(monkeypatch):
    _patch(
        monkeypatch,
        semantic=[(1, 1.0), (2, 0.0)],
        bm25=[(2, 10.0), (3, 5.0)],
        reranker=FakeReranker(error=True),
    )
    result = _run(R.retrieve("q"))

    assert result.rerank_used is False
    # 降级不过滤阈值，按 RRF 候选顺序全保留，分数记 1.0
    assert [c.chunk_id for c in result.chunks] == [2, 1, 3]
    assert all(c.score == 1.0 for c in result.chunks)


def test_mock_mode_skips_paid_calls(monkeypatch):
    values = dict(BASE_VALUES, **{"debug.mock_third_party": True})
    _patch(
        monkeypatch,
        semantic=[(1, 1.0)],
        bm25=[(2, 10.0), (3, 5.0)],
        reranker=FakeReranker(error=True),  # 若被调用会抛错，mock 下不应触发
        values=values,
    )
    result = _run(R.retrieve("q"))

    assert result.mocked is True
    assert result.rerank_used is False
    assert result.semantic_hits == []  # 语义路被跳过
    assert [c.chunk_id for c in result.chunks] == [2, 3]  # BM25 照常生效


def test_empty_kb_returns_empty_result(monkeypatch):
    _patch(monkeypatch, semantic=[], bm25=[], reranker=FakeReranker())
    result = _run(R.retrieve("q"))
    assert result.chunks == []
    assert result.fused_hits == []


def test_stale_index_id_skipped_when_db_missing(monkeypatch):
    # chunk 99 在假 DB 里不存在（模拟索引与库失步），应被跳过而不是报错
    _patch(
        monkeypatch,
        semantic=[(99, 1.0), (1, 0.5)],
        bm25=[],
        reranker=FakeReranker(pairs=[(0, 0.9)]),
    )
    result = _run(R.retrieve("q"))
    assert [c.chunk_id for c in result.chunks] == [1]
