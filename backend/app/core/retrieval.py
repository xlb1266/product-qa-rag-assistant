"""混合检索编排：语义搜索(FAISS) + BM25 关键词搜索 -> RRF 融合 -> qwen3-rerank 重排。

流程：
1. 两路并行召回各 semantic_top_n 条，分数各自归一化到 [0, 1]
   - 语义：余弦相似度 [-1, 1] 线性映射到 [0, 1]
   - BM25：按本次查询的最大分数做 min-max 归一化
2. RRF 融合去重（同一 chunk 两路命中分数累加），取分数最高的
   candidate_top_n 条作为重排候选集
3. 候选集送 qwen3-rerank 重排，保留 relevance_score >= rerank_threshold
   的前 rerank_top_k 条，按分数降序返回

检索参数（召回/候选/阈值/条数）来自运行时配置（调试页可改、热生效），
RRF_K 为业界常用平滑常数，不开放页面修改。

Mock 模式（debug.mock_third_party=true）：跳过两个付费第三方调用
（query 向量化、rerank），语义路返回空、重排降级为 RRF 排序，
BM25 为纯本地索引照常执行——重放时不花钱也能看到真实检索行为。
"""

from dataclasses import dataclass, field

from app.core.bm25 import get_bm25_index
from app.core.embeddings import EmbeddingError, get_embedding_client
from app.core.reranker import RerankError, get_reranker
from app.core.runtime_config import get_runtime
from app.core.vector_store import get_vector_store
from app.db.database import get_db

RRF_K = 60  # RRF 平滑常数，业界常用 60


@dataclass
class SearchHit:
    chunk_id: int
    score: float


@dataclass
class RetrievedChunk:
    chunk_id: int
    document_id: int
    document_title: str
    seq: int
    heading_path: str
    content: str
    score: float  # 最终相关性分数（rerank 分数；降级时为 RRF 归一化分数）
    semantic_score: float | None = None
    bm25_score: float | None = None
    rrf_score: float | None = None


@dataclass
class RetrievalResult:
    query: str
    semantic_hits: list[SearchHit] = field(default_factory=list)
    bm25_hits: list[SearchHit] = field(default_factory=list)
    fused_hits: list[SearchHit] = field(default_factory=list)
    chunks: list[RetrievedChunk] = field(default_factory=list)
    rerank_used: bool = True  # False 表示重排失败或被 mock 跳过，已降级用 RRF 排序
    mocked: bool = False  # True 表示本次检索跳过了第三方调用（mock 模式）


async def retrieve(query: str) -> RetrievalResult:
    """完整混合检索链路。Embedding 失败直接抛出（无法进行语义检索）。"""
    rt = get_runtime()
    semantic_top_n: int = rt.get("retrieval.semantic_top_n")
    candidate_top_n: int = rt.get("retrieval.candidate_top_n")
    rerank_top_k: int = rt.get("retrieval.rerank_top_k")
    rerank_threshold: float = rt.get("retrieval.rerank_threshold")
    mocked: bool = bool(rt.get("debug.mock_third_party"))

    result = RetrievalResult(query=query, mocked=mocked)

    # 1) 语义检索：query 向量化后查 FAISS，余弦分数映射到 [0, 1]
    #    mock 模式跳过百炼向量化，语义路返回空
    semantic_scores: dict[int, float] = {}
    if not mocked:
        vector = (await get_embedding_client().embed([query]))[0]
        raw_semantic = get_vector_store().search(vector, k=semantic_top_n)
        semantic_scores = {
            cid: min(max((s + 1.0) / 2.0, 0.0), 1.0) for cid, s in raw_semantic
        }
    result.semantic_hits = [
        SearchHit(cid, s)
        for cid, s in sorted(semantic_scores.items(), key=lambda x: -x[1])
    ]

    # 2) BM25 检索：原始分数按本次最大值归一化到 [0, 1]（纯本地索引，mock 下照常）
    raw_bm25 = get_bm25_index().search(query, top_n=semantic_top_n)
    max_bm25 = max((s for _, s in raw_bm25), default=0.0)
    bm25_scores = (
        {cid: s / max_bm25 for cid, s in raw_bm25} if max_bm25 > 0 else {}
    )
    result.bm25_hits = [
        SearchHit(cid, s)
        for cid, s in sorted(bm25_scores.items(), key=lambda x: -x[1])
    ]

    # 3) RRF 融合：score = Σ 1/(k + rank)，两路均第一的理论最大值为 2/(k+1)，
    #    除以它归一化到 [0, 1]；dict 合并天然去重（同一 chunk 累加）
    rrf: dict[int, float] = {}
    for hits in (result.semantic_hits, result.bm25_hits):
        for rank, hit in enumerate(hits, start=1):
            rrf[hit.chunk_id] = rrf.get(hit.chunk_id, 0.0) + 1.0 / (RRF_K + rank)
    rrf_max = 2.0 / (RRF_K + 1)
    result.fused_hits = [
        SearchHit(cid, s / rrf_max)
        for cid, s in sorted(rrf.items(), key=lambda x: -x[1])
    ]

    # 4) 取融合 top 候选，加载 chunk 详情
    candidate_ids = [h.chunk_id for h in result.fused_hits[:candidate_top_n]]
    chunks = _load_chunks(candidate_ids)
    # 防御索引与数据库失步（如向量已删但索引未同步）：跳过已不存在的 chunk
    candidate_ids = [cid for cid in candidate_ids if cid in chunks]
    if not candidate_ids:
        return result

    # 5) 重排：mock 模式或失败则降级用 RRF 排序，保证链路可用
    if mocked:
        result.rerank_used = False
        rerank_pairs = [
            (candidate_ids.index(cid), 1.0) for cid in candidate_ids[:rerank_top_k]
        ]
    else:
        try:
            rerank_pairs = await get_reranker().rerank(
                query,
                [chunks[cid].content for cid in candidate_ids],
                top_n=rerank_top_k,
            )
        except RerankError:
            result.rerank_used = False
            rerank_pairs = [
                (candidate_ids.index(cid), 1.0) for cid in candidate_ids[:rerank_top_k]
            ]

    final: list[RetrievedChunk] = []
    for idx, score in rerank_pairs:
        chunk = chunks[candidate_ids[idx]]
        # mock / 降级路径没有真实 rerank 分数，不做阈值过滤
        if result.rerank_used and score < rerank_threshold:
            continue
        chunk.score = round(score, 4)
        chunk.semantic_score = (
            round(semantic_scores.get(chunk.chunk_id, 0.0), 4)
            if semantic_scores
            else None
        )
        chunk.bm25_score = (
            round(bm25_scores.get(chunk.chunk_id, 0.0), 4) if bm25_scores else None
        )
        chunk.rrf_score = round(rrf.get(chunk.chunk_id, 0.0) / rrf_max, 4)
        final.append(chunk)

    # 按相关度降序、chunk_id 去重（rerank 正常不会重复，防御异常输出）
    seen: set[int] = set()
    deduped: list[RetrievedChunk] = []
    for chunk in sorted(final, key=lambda c: -c.score):
        if chunk.chunk_id not in seen:
            seen.add(chunk.chunk_id)
            deduped.append(chunk)
    result.chunks = deduped[:rerank_top_k]
    return result


def _load_chunks(chunk_ids: list[int]) -> dict[int, RetrievedChunk]:
    """按 id 批量加载 chunk 详情（含所属文档标题）。"""
    if not chunk_ids:
        return {}
    placeholders = ",".join("?" for _ in chunk_ids)
    rows = get_db().query(
        "SELECT c.id, c.document_id, c.seq, c.heading_path, c.content,"
        " d.title AS document_title"
        f" FROM chunks c JOIN documents d ON d.id = c.document_id"
        f" WHERE c.id IN ({placeholders})",
        tuple(chunk_ids),
    )
    return {
        r["id"]: RetrievedChunk(
            chunk_id=r["id"],
            document_id=r["document_id"],
            document_title=r["document_title"],
            seq=r["seq"],
            heading_path=r["heading_path"],
            content=r["content"],
            score=0.0,
        )
        for r in rows
    }
