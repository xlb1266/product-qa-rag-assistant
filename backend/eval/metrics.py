"""评测指标计算（独立函数库，供 run_eval.py 与后续阶段复用）。

约定：
- 命中判定基于 chunk 内容是否包含全部期望关键词（子串匹配），
  不绑定 chunk_id，切分策略变化后仍可复用；
- 检索结果统一传入"已按相关度降序排列的内容列表"，
  排序由调用方保证（RRF 排序或 rerank 排序均可）。

指标：
- recall_at_k / mrr / ndcg_at_k：检索单条 query 的指标，聚合用 mean_* 系列
- answer_layer：回答层单条判定（准确率 / 拒答正确率）
- threshold_analysis：rerank 分数分布 + 各候选阈值的误伤统计
"""

import math
from dataclasses import dataclass, field


def content_hit(content: str, expected_keywords: list[str]) -> bool:
    """chunk 内容包含全部期望关键词即视为命中（子串匹配，不区分大小写交给出题方）。"""
    if not expected_keywords:
        return False
    return all(kw in content for kw in expected_keywords)


# ---------------------------------------------------------------------------
# 检索层指标（单条 query -> 数值，聚合函数在下方）
# ---------------------------------------------------------------------------


def recall_at_k(ranked_contents: list[str], expected_keywords: list[str], k: int) -> float:
    """期望内容是否出现在检索结果前 K：命中 1.0，否则 0.0。"""
    return 1.0 if any(content_hit(c, expected_keywords) for c in ranked_contents[:k]) else 0.0


def mrr(ranked_contents: list[str], expected_keywords: list[str]) -> float:
    """倒数排名 MRR：第一个命中 chunk 排名的倒数，全不命中为 0。"""
    for rank, content in enumerate(ranked_contents, start=1):
        if content_hit(content, expected_keywords):
            return 1.0 / rank
    return 0.0


def ndcg_at_k(ranked_contents: list[str], expected_keywords: list[str], k: int) -> float:
    """二值相关性 nDCG@K：命中 chunk 相关度 1，其余 0。"""
    dcg = sum(
        1.0 / math.log2(rank + 1)
        for rank, content in enumerate(ranked_contents[:k], start=1)
        if content_hit(content, expected_keywords)
    )
    # 理想 DCG：命中若干条全部排在最前（二值相关下 IDCG 只与命中条数有关，
    # 但检索时命中条数未知，统一按"至少 1 条相关"计算，等价于 1.0）
    idcg = 1.0 / math.log2(2)
    return dcg / idcg if idcg > 0 else 0.0


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def aggregate_retrieval_metrics(
    ranked_by_query: dict[str, list[str]], items_by_query: dict[str, list[str]], k_list: list[int]
) -> dict[str, float]:
    """批量聚合检索指标。

    ranked_by_query: query -> 按相关度降序的 chunk 内容列表
    items_by_query:  query -> 该 query 的期望关键词（answerable 才参与计算）
    """
    metrics: dict[str, float] = {}
    for k in k_list:
        metrics[f"recall@{k}"] = mean(
            [recall_at_k(ranked_by_query[q], items_by_query[q], k) for q in items_by_query]
        )
        metrics[f"ndcg@{k}"] = mean(
            [ndcg_at_k(ranked_by_query[q], items_by_query[q], k) for q in items_by_query]
        )
    metrics["mrr"] = mean([mrr(ranked_by_query[q], items_by_query[q]) for q in items_by_query])
    return metrics


# ---------------------------------------------------------------------------
# 回答层指标
# ---------------------------------------------------------------------------


def answer_correct(item: dict, useful: bool, content: str) -> bool:
    """单条回答判定。

    answerable：useful=true 且回答包含全部期望要点
    unanswerable：useful=false（正确拒答）
    ambiguous：不做硬性判定，返回 True 仅表示"未崩溃"，不计入硬指标
    """
    if item["type"] == "answerable":
        if not useful:
            return False
        points = item.get("expected_answer_points") or []
        return all(p in content for p in points)
    if item["type"] == "unanswerable":
        return not useful
    return True


def aggregate_answer_metrics(results: list[dict]) -> dict[str, float]:
    """聚合回答层指标。results 每项含 type / useful / content / expected_answer_points / correct。"""
    answerable = [r for r in results if r["type"] == "answerable"]
    unanswerable = [r for r in results if r["type"] == "unanswerable"]
    return {
        "answer_accuracy": mean([1.0 if r["correct"] else 0.0 for r in answerable]),
        "refusal_accuracy": mean([1.0 if r["correct"] else 0.0 for r in unanswerable]),
        "answerable_refused_rate": mean(
            [1.0 if not r["useful"] else 0.0 for r in answerable]
        ),
        "unanswerable_answered_rate": mean(
            [1.0 if r["useful"] else 0.0 for r in unanswerable]
        ),
    }


# ---------------------------------------------------------------------------
# 生成段裁判指标聚合（v2 新增）
# ---------------------------------------------------------------------------


def aggregate_judge_metrics(verdicts: list[dict]) -> dict:
    """聚合裁判逐条结果。

    verdicts 每项含：faithfulness / answer_relevance（0~1 float）、
    judge_error（bool，该条裁判是否最终失败）。失败条目不进均值，单独计数。
    """
    ok = [v for v in verdicts if not v.get("judge_error")]
    errors = [v for v in verdicts if v.get("judge_error")]
    return {
        "faithfulness_mean": round(mean([v["faithfulness"] for v in ok]), 4),
        "answer_relevance_mean": round(mean([v["answer_relevance"] for v in ok]), 4),
        "judged_count": len(ok),
        "judge_error_count": len(errors),
        "faithfulness_distribution": score_distribution([v["faithfulness"] for v in ok]),
        "answer_relevance_distribution": score_distribution(
            [v["answer_relevance"] for v in ok]
        ),
    }


def score_distribution(scores: list[float]) -> dict[str, int]:
    """0~1 分数分段计数：满分 / 0.8~1 / 0.5~0.8 / <0.5，用于看裁判打分是否扎堆。"""
    return {
        "1.0": sum(1 for s in scores if s >= 1.0),
        "0.8-1.0": sum(1 for s in scores if 0.8 <= s < 1.0),
        "0.5-0.8": sum(1 for s in scores if 0.5 <= s < 0.8),
        "<0.5": sum(1 for s in scores if s < 0.5),
    }


# ---------------------------------------------------------------------------
# rerank 阈值分析
# ---------------------------------------------------------------------------


@dataclass
class RerankScoreRecord:
    """一条候选的 rerank 分数与真实相关性标签（由期望关键词判定）。"""

    query_id: str
    score: float
    relevant: bool
    content: str = ""


def quantiles(values: list[float], qs: list[float]) -> dict[str, float]:
    """线性插值分位数，q 取 0~1。"""
    if not values:
        return {f"p{int(q * 100)}": 0.0 for q in qs}
    ordered = sorted(values)
    result: dict[str, float] = {}
    for q in qs:
        pos = q * (len(ordered) - 1)
        lo, hi = math.floor(pos), math.ceil(pos)
        result[f"p{int(q * 100)}"] = round(ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo), 4)
    return result


def histogram(values: list[float], bucket: float = 0.1) -> dict[str, int]:
    """分数直方图，按 bucket 宽度分桶，桶名如 "0.5-0.6"。"""
    hist: dict[str, int] = {}
    for v in values:
        lo = math.floor(v / bucket) * bucket
        hi = min(lo + bucket, 1.0)
        name = f"{lo:.1f}-{hi:.1f}"
        hist[name] = hist.get(name, 0) + 1
    return dict(sorted(hist.items()))


def threshold_analysis(
    records: list[RerankScoreRecord], candidate_thresholds: list[float]
) -> dict:
    """rerank 分数分布与阈值误伤分析。

    对每个候选阈值统计：
    - relevant_below：真相关候选被阈值砍掉的数量（漏，越少越好）
    - irrelevant_kept：不相关候选被阈值放过的数量（混入噪音，越少越好）
    """
    scores = [r.score for r in records]
    relevant_scores = [r.score for r in records if r.relevant]
    irrelevant_scores = [r.score for r in records if not r.relevant]
    return {
        "total_candidates": len(records),
        "relevant_count": len(relevant_scores),
        "irrelevant_count": len(irrelevant_scores),
        "all_scores": quantiles(scores, [0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0]),
        "relevant_scores": quantiles(relevant_scores, [0.0, 0.25, 0.5, 0.75, 1.0]),
        "irrelevant_scores": quantiles(irrelevant_scores, [0.0, 0.25, 0.5, 0.75, 1.0]),
        "histogram_all": histogram(scores),
        "histogram_relevant": histogram(relevant_scores),
        "histogram_irrelevant": histogram(irrelevant_scores),
        "per_threshold": [
            {
                "threshold": t,
                "relevant_below": sum(1 for s in relevant_scores if s < t),
                "irrelevant_kept": sum(1 for s in irrelevant_scores if s >= t),
                "relevant_kept": sum(1 for s in relevant_scores if s >= t),
            }
            for t in candidate_thresholds
        ],
    }
