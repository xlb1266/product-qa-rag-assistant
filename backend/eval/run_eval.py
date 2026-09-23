"""RAG 客服离线评测入口。

走真实链路（进程内直接调用 retrieve / generate_answer，不起服务、不发 HTTP）：
1. 检索层指标：Recall@4/8、MRR、nDCG@4（RRF 排序与 rerank 排序各算一份）
2. rerank 价值：RRF top4 vs rerank top4 的指标差值
3. rerank 分数分布 + 各阈值下的误伤统计（0.6 阈值是否合理）
4. 回答层指标：answerable 准确率、unanswerable 拒答正确率、ambiguous 行为分布
5. 生成段裁判（v2）：对 answerable 且 useful=true 的回答，用当次检索到的上下文
   评 Faithfulness（忠实度，防幻觉）与 Answer Relevance（切题度）
6. 参数网格（--grid 开启）：semantic_top_n {5,10,20} × rerank_threshold {0.5,0.6,0.7}
   只跑检索层指标，一条 LLM 都不调；参数通过覆盖内存单例实现，不动业务代码

成本控制：embedding / rerank 调用在评测进程内按内容缓存
（同一 query 多个网格配置只向量化一次；同一候选集跨阈值只重排一次），
DeepSeek 每条回答一次、裁判按需另计。正式评测必须关闭 mock 模式。

用法（在 backend 目录下）：
  uv run python eval/run_eval.py                  # 全量评测（检索+回答层，不调裁判）
  uv run python eval/run_eval.py --limit 3        # 冒烟：每类前 3 条
  uv run python eval/run_eval.py --retrieval-only # 只跑检索段确定性指标（不调任何 LLM，频繁回归用）
  uv run python eval/run_eval.py --judge          # 全量评测 + 生成段裁判全量（手动模式，LLM 调用最多）
  uv run python eval/run_eval.py --ci             # CI 门禁：检索段全量 + 生成段抽样裁判，
                                                  #   与 thresholds.json 对比，任一跌破退出码 1
  uv run python eval/run_eval.py --grid           # 附加跑参数网格（semantic_top_n × rerank_threshold）

--retrieval-only 是 --no-answer 的新名字，两者等价。
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.answer import FALLBACK_REPLY, AnswerResult, assess_answerability, generate_answer  # noqa: E402
from app.core.embeddings import get_embedding_client  # noqa: E402
from app.core.llm import get_llm_client  # noqa: E402
from app.core.reranker import RerankError, get_reranker  # noqa: E402
from app.core.retrieval import RetrievalResult, retrieve  # noqa: E402
from app.core.runtime_config import get_runtime  # noqa: E402
from app.db.database import get_db  # noqa: E402

import judge as J  # noqa: E402
import metrics as M  # noqa: E402

EVAL_DIR = Path(__file__).parent
RESULTS_PATH = Path(__file__).resolve().parents[2] / "docs" / "eval"
THRESHOLDS_PATH = EVAL_DIR / "thresholds.json"

K_LIST = [4, 8]
GRID_SEMANTIC_TOP_N = [5, 10, 20]
GRID_RERANK_THRESHOLD = [0.5, 0.6, 0.7]
CANDIDATE_THRESHOLDS = [0.4, 0.5, 0.6, 0.7, 0.8]  # 阈值分析扫描点
CI_JUDGE_SAMPLE = 5  # CI 门禁模式生成段抽样条数

_api_stats = {"embed": 0, "rerank": 0, "llm": 0}


# ---------------------------------------------------------------------------
# API 调用缓存包装（仅包装单例实例方法，不改动业务代码）
# ---------------------------------------------------------------------------


def install_api_caches() -> None:
    """给 embedding / rerank / LLM 单例包一层进程内缓存与计数。

    rerank 缓存固定以 top_n=None 调底层并缓存全量分数，再按请求切片——
    这样同一 (query, 候选集) 在 retrieve(top_n=4) 与阈值分析(top_n=None)
    之间共享同一次 API 调用，分数完全一致。
    """
    ec = get_embedding_client()
    orig_embed = ec.embed
    embed_cache: dict[tuple, list] = {}

    async def cached_embed(texts: list[str]) -> list[list[float]]:
        key = tuple(texts)
        if key not in embed_cache:
            _api_stats["embed"] += 1
            embed_cache[key] = await orig_embed(list(texts))
        return embed_cache[key]

    ec.embed = cached_embed

    rk = get_reranker()
    orig_rerank = rk.rerank
    rerank_cache: dict[tuple, list] = {}

    async def cached_rerank(query: str, documents: list[str], top_n: int | None = None):
        key = (query, tuple(documents))
        if key not in rerank_cache:
            _api_stats["rerank"] += 1
            rerank_cache[key] = await orig_rerank(query, list(documents), top_n=None)
        full = rerank_cache[key]
        return full[:top_n] if top_n is not None else full

    rk.rerank = cached_rerank

    llm = get_llm_client()
    orig_chat = llm.chat

    async def counted_chat(*args, **kwargs):
        _api_stats["llm"] += 1
        return await orig_chat(*args, **kwargs)

    llm.chat = counted_chat


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def load_contents(chunk_ids: list[int]) -> dict[int, str]:
    """按 id 批量查 chunk 正文（评测侧只读，不动业务层）。"""
    if not chunk_ids:
        return {}
    placeholders = ",".join("?" for _ in chunk_ids)
    rows = get_db().query(
        f"SELECT id, content FROM chunks WHERE id IN ({placeholders})",
        tuple(chunk_ids),
    )
    return {r["id"]: r["content"] for r in rows}


def fused_ranked_contents(result: RetrievalResult, depth: int) -> list[str]:
    """RRF 融合排序前 depth 位的内容列表（rerank 之前的排序基线）。"""
    ids = [h.chunk_id for h in result.fused_hits[:depth]]
    contents = load_contents(ids)
    return [contents[cid] for cid in ids if cid in contents]


async def full_rerank_scores(
    item: dict, result: RetrievalResult
) -> list[M.RerankScoreRecord]:
    """对融合候选集拿全量 rerank 分数，打上相关性标签（供阈值分析）。

    doc_list 的构造方式与 retrieve() 内部完全一致（融合 top candidate_top_n、
    过滤已不存在的 chunk），保证命中 rerank 缓存、分数与真实链路一致。
    """
    candidate_top_n = get_runtime().get("retrieval.candidate_top_n")
    ids = [h.chunk_id for h in result.fused_hits[:candidate_top_n]]
    contents = load_contents(ids)
    if not contents:
        return []
    doc_list = [contents[cid] for cid in ids if cid in contents]
    try:
        pairs = await get_reranker().rerank(item["query"], doc_list, top_n=None)
    except RerankError as e:
        print(f"[WARN] {item['id']} rerank 失败，跳过阈值分析样本：{e}")
        return []
    records = []
    for idx, score in pairs:
        content = doc_list[idx]
        records.append(
            M.RerankScoreRecord(
                query_id=item["id"],
                score=score,
                relevant=M.content_hit(content, item["expected_keywords"]),
                content=content[:80],
            )
        )
    return records


# ---------------------------------------------------------------------------
# 评测各环节
# ---------------------------------------------------------------------------


async def run_retrieval_pass(items: list[dict]) -> dict:
    """主评测：真实链路检索 + 全量 rerank 分数收集。"""
    per_item: list[dict] = []
    rerank_records: list[M.RerankScoreRecord] = []
    for item in items:
        if item["type"] != "answerable":
            continue
        result = await retrieve(item["query"])
        rrf_ranked = fused_ranked_contents(result, depth=max(K_LIST))
        rerank_ranked = [c.content for c in result.chunks]
        records = await full_rerank_scores(item, result)
        rerank_records.extend(records)

        rrf_hit_rank = next(
            (i + 1 for i, c in enumerate(rrf_ranked) if M.content_hit(c, item["expected_keywords"])),
            None,
        )
        rerank_hit_rank = next(
            (i + 1 for i, c in enumerate(rerank_ranked) if M.content_hit(c, item["expected_keywords"])),
            None,
        )
        per_item.append(
            {
                "id": item["id"],
                "query": item["query"],
                "expected_section": item.get("expected_section"),
                "rerank_used": result.rerank_used,
                "rrf_hit_rank": rrf_hit_rank,
                "rerank_hit_rank": rerank_hit_rank,
                "rrf_recall@4": M.recall_at_k(rrf_ranked, item["expected_keywords"], 4),
                "rrf_recall@8": M.recall_at_k(rrf_ranked, item["expected_keywords"], 8),
                "rerank_recall@4": M.recall_at_k(rerank_ranked, item["expected_keywords"], 4),
                "rrf_mrr": M.mrr(rrf_ranked, item["expected_keywords"]),
                "rerank_mrr": M.mrr(rerank_ranked, item["expected_keywords"]),
                "rrf_ndcg@4": M.ndcg_at_k(rrf_ranked, item["expected_keywords"], 4),
                "rerank_ndcg@4": M.ndcg_at_k(rerank_ranked, item["expected_keywords"], 4),
                "rerank_final_scores": [round(c.score, 4) for c in result.chunks],
                "candidate_rerank_scores": [round(r.score, 4) for r in records],
                "expected_chunk_hit_score": next(
                    (round(r.score, 4) for r in records if r.relevant), None
                ),
            }
        )
        flag = "✓" if rerank_hit_rank else ("RRF✓" if rrf_hit_rank else "✗")
        print(f"  [{item['id']}] {flag} RRF排名={rrf_hit_rank} rerank排名={rerank_hit_rank} "
              f"候选分数={[r.score for r in records][:4]}")
    return {"per_item": per_item, "rerank_records": rerank_records}


def aggregate_retrieval(per_item: list[dict]) -> dict:
    """把逐条结果聚合成指标表（RRF 与 rerank 两列）。"""

    def mean_of(key: str) -> float:
        return M.mean([r[key] for r in per_item])

    return {
        "RRF排序": {
            "recall@4": round(mean_of("rrf_recall@4"), 4),
            "recall@8": round(mean_of("rrf_recall@8"), 4),
            "mrr": round(mean_of("rrf_mrr"), 4),
            "ndcg@4": round(mean_of("rrf_ndcg@4"), 4),
        },
        "rerank后": {
            "recall@4": round(mean_of("rerank_recall@4"), 4),
            "recall@8": round(mean_of("rerank_recall@4"), 4),  # rerank_top_k=4，@8 等同 @4
            "mrr": round(mean_of("rerank_mrr"), 4),
            "ndcg@4": round(mean_of("rerank_ndcg@4"), 4),
        },
    }


async def run_answer_pass(
    items: list[dict], judge_ids: set[str] | None = None
) -> dict:
    """回答层评测：真实检索结果喂给 generate_answer。

    judge_ids 非空时，对其中 answerable 且 useful=true 的条目调用裁判打分；
    裁判上下文直接取当次检索的 ret.chunks，不重新检索（保证评的就是这次回答）。
    useful=false 的 answerable 条目（误拒）没有事实陈述可评，跳过裁判，
    由误拒率指标单独覆盖；unanswerable / ambiguous 不打裁判分。

    阶段 2 起，回答前先过 assess_answerability 代码层预判（与线上 chat.py
    同一实现）：被拦截的条目不调 DeepSeek，直接记为兜底话术拒答。
    """
    results: list[dict] = []
    judge_verdicts: list[dict] = []
    precheck_blocked = 0
    for item in items:
        ret = await retrieve(item["query"])
        answerable, _ = assess_answerability(ret.chunks, ret.rerank_used)
        if answerable:
            ans = await generate_answer(history=[], message=item["query"], chunks=ret.chunks)
            blocked = False
        else:
            ans = AnswerResult(useful=False, content=FALLBACK_REPLY)  # 代码层拦截，未进 LLM
            blocked = True
            precheck_blocked += 1
        correct = M.answer_correct(item, ans.useful, ans.content)
        results.append(
            {
                "id": item["id"],
                "query": item["query"],
                "type": item["type"],
                "useful": ans.useful,
                "content": ans.content,
                "correct": correct,
                "precheck_blocked": blocked,
                "chunks_used": len(ret.chunks),
            }
        )
        mark = "✓" if correct else ("△" if item["type"] == "ambiguous" else "✗")
        suffix = " | 预判拦截" if blocked else ""
        if judge_ids and item["id"] in judge_ids and item["type"] == "answerable" and ans.useful:
            contexts = [c.content for c in ret.chunks]
            try:
                verdict = await J.judge_answer_with_retry(item["query"], contexts, ans.content)
                judge_verdicts.append(
                    {
                        "id": item["id"],
                        "query": item["query"],
                        "answer": ans.content,
                        "contexts": contexts,
                        "faithfulness": verdict.faithfulness,
                        "answer_relevance": verdict.answer_relevance,
                        "reason": verdict.reason,
                        "judge_error": False,
                    }
                )
                suffix = f" | 忠实度={verdict.faithfulness} 切题度={verdict.answer_relevance}"
            except J.JudgeError as e:
                judge_verdicts.append(
                    {"id": item["id"], "judge_error": True, "error": str(e)}
                )
                suffix = " | 裁判失败（已记 judge_error）"
        print(f"  [{item['id']}] {mark} useful={ans.useful} | {ans.content[:50]}{suffix}")
    return {
        "metrics": {k: round(v, 4) for k, v in M.aggregate_answer_metrics(results).items()},
        "precheck_blocked": precheck_blocked,
        "ambiguous_breakdown": {
            "total": len([r for r in results if r["type"] == "ambiguous"]),
            "refused": len([r for r in results if r["type"] == "ambiguous" and not r["useful"]]),
        },
        "per_item": results,
        "judge_verdicts": judge_verdicts,
    }


async def run_grid(items: list[dict]) -> list[dict]:
    """参数网格：semantic_top_n × rerank_threshold，只跑检索层。

    semantic_top_n 通过覆盖内存单例的参数值实现（绕过 set_many，
    不写库不留痕，跑完恢复）。rerank_threshold 不改参数、不重复调 API：
    对每条 query 取一次全量 rerank 分数（缓存命中），按分数降序后
    逐阈值重算保留集——与真实链路的过滤逻辑等价。
    """
    rt = get_runtime()
    candidate_top_n = rt.get("retrieval.candidate_top_n")
    rerank_top_k = rt.get("retrieval.rerank_top_k")
    saved_sn = rt.get("retrieval.semantic_top_n")
    answerable = [it for it in items if it["type"] == "answerable"]
    rows: list[dict] = []
    try:
        for sn in GRID_SEMANTIC_TOP_N:
            rt._values["retrieval.semantic_top_n"] = sn
            rrf_recall8: list[float] = []
            rrf_mrrs: list[float] = []
            per_th: dict[float, dict[str, list[float]]] = {
                th: {"recall@4": [], "mrr": [], "kept": []} for th in GRID_RERANK_THRESHOLD
            }
            for item in answerable:
                result = await retrieve(item["query"])
                rrf_ranked = fused_ranked_contents(result, depth=max(K_LIST))
                rrf_recall8.append(M.recall_at_k(rrf_ranked, item["expected_keywords"], 8))
                rrf_mrrs.append(M.mrr(rrf_ranked, item["expected_keywords"]))

                ids = [h.chunk_id for h in result.fused_hits[:candidate_top_n]]
                contents = load_contents(ids)
                doc_list = [contents[cid] for cid in ids if cid in contents]
                if not doc_list:
                    continue
                try:
                    pairs = await get_reranker().rerank(item["query"], doc_list, top_n=None)
                except RerankError as e:
                    print(f"[WARN] {item['id']} rerank 失败，网格样本跳过：{e}")
                    continue
                scored = sorted(pairs, key=lambda p: -p[1])
                for th in GRID_RERANK_THRESHOLD:
                    kept = [doc_list[idx] for idx, s in scored if s >= th][:rerank_top_k]
                    per_th[th]["recall@4"].append(
                        M.recall_at_k(kept, item["expected_keywords"], 4)
                    )
                    per_th[th]["mrr"].append(M.mrr(kept, item["expected_keywords"]))
                    per_th[th]["kept"].append(len(kept))

            rows.append({
                "semantic_top_n": sn,
                "rrf_recall@8": round(M.mean(rrf_recall8), 4),
                "rrf_mrr": round(M.mean(rrf_mrrs), 4),
                "per_threshold": [
                    {
                        "rerank_threshold": th,
                        "rerank_recall@4": round(M.mean(v["recall@4"]), 4),
                        "rerank_mrr": round(M.mean(v["mrr"]), 4),
                        "avg_kept": round(M.mean(v["kept"]), 2),
                    }
                    for th, v in per_th.items()
                ],
            })
            print(f"  semantic_top_n={sn}: RRF recall@8={rows[-1]['rrf_recall@8']} 已算完")
    finally:
        rt._values["retrieval.semantic_top_n"] = saved_sn
    return rows


# ---------------------------------------------------------------------------
# CI 门禁（v2）
# ---------------------------------------------------------------------------


def load_thresholds() -> dict:
    return json.loads(THRESHOLDS_PATH.read_text(encoding="utf-8"))


def sample_answerable(items: list[dict], n: int) -> list[dict]:
    """确定性抽样：answerable 按 expected_section 分组保持数据集顺序，组间轮转各取一条。

    不用随机抽样——CI 需要可复现，同样的测试集每次抽到同样的条目。
    轮转保证抽样覆盖尽量多的知识库章节，而不是扎堆在前几节。
    """
    groups: dict[str, list[dict]] = {}
    for it in items:
        if it["type"] == "answerable":
            groups.setdefault(it.get("expected_section") or it["id"], []).append(it)
    sampled: list[dict] = []
    round_idx = 0
    while len(sampled) < n:
        picked = False
        for key in sorted(groups):
            if round_idx < len(groups[key]):
                sampled.append(groups[key][round_idx])
                picked = True
                if len(sampled) == n:
                    break
        if not picked:
            break
        round_idx += 1
    return sampled


def ci_gate_check(metrics: dict, thresholds: dict) -> list[dict]:
    """逐项对比指标与阈值下限，返回检查结果列表。

    metrics 键形如 "retrieval.rerank_recall_at_4"，与 thresholds.json 的层级一一对应。
    """
    rows: list[dict] = []
    for name, value in metrics.items():
        section, key = name.split(".", 1)
        lower = thresholds[section][key]
        rows.append(
            {
                "metric": name,
                "value": round(value, 4),
                "threshold": lower,
                "pass": value >= lower,
            }
        )
    return rows


def print_gate(rows: list[dict]) -> list[str]:
    failures: list[str] = []
    for row in rows:
        mark = "PASS" if row["pass"] else "FAIL"
        print(f"  [{mark}] {row['metric']} = {row['value']}（下限 {row['threshold']}）")
        if not row["pass"]:
            failures.append(f"{row['metric']} = {row['value']} < 下限 {row['threshold']}")
    return failures


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="RAG 客服离线评测")
    p.add_argument("--limit", type=int, default=0, help="每类问题只取前 N 条（冒烟用）")
    p.add_argument("--no-answer", action="store_true", help="跳过回答层（不调 DeepSeek）")
    p.add_argument("--retrieval-only", action="store_true",
                   help="只跑检索段确定性指标（不调任何 LLM），= --no-answer 的新名字，频繁回归用")
    p.add_argument("--judge", action="store_true",
                   help="全量评测 + 生成段裁判全量（手动模式，每条 answerable 多一次裁判调用）")
    p.add_argument("--ci", action="store_true",
                   help="CI 门禁模式：检索段全量 + 生成段抽样裁判，与 thresholds.json 对比，"
                        "任一跌破退出码 1、全过退出码 0")
    p.add_argument("--grid", action="store_true", help="附加跑参数网格（semantic_top_n × rerank_threshold）")
    args = p.parse_args()
    if args.retrieval_only and args.no_answer:
        p.error("--retrieval-only 与 --no-answer 等价，只能传一个")
    if args.ci and (args.grid or args.judge or args.no_answer or args.retrieval_only):
        p.error("--ci 是固定流程的门禁模式，不与 --grid / --judge / --no-answer / --retrieval-only 组合")
    return args


def select_items(limit: int) -> list[dict]:
    items = json.loads((EVAL_DIR / "dataset.json").read_text(encoding="utf-8"))["items"]
    if limit > 0:
        by_type: dict[str, list[dict]] = {}
        for it in items:
            by_type.setdefault(it["type"], []).append(it)
        items = [it for lst in by_type.values() for it in lst[:limit]]
    return items


async def main() -> None:
    args = parse_args()
    rt = get_runtime()
    if rt.get("debug.mock_third_party"):
        print("[错误] 当前为 mock 模式（debug.mock_third_party=true），"
              "语义检索返回空、重排被跳过，不能用于正式评测。"
              "请先在调试页关闭 mock 后重跑。")
        sys.exit(1)

    retrieval_only = args.retrieval_only or args.no_answer

    items = select_items(args.limit)
    answerable = [it for it in items if it["type"] == "answerable"]
    print(f"=== RAG 客服离线评测 ===")
    mode = "CI 门禁" if args.ci else ("检索段 only" if retrieval_only else
                                     ("全量+裁判" if args.judge else "全量"))
    print(f"模式：{mode}")
    print(f"测试集：{len(items)} 条（answerable={len(answerable)}, "
          f"unanswerable={len([i for i in items if i['type']=='unanswerable'])}, "
          f"ambiguous={len([i for i in items if i['type']=='ambiguous'])}）")
    print(f"链路参数：semantic_top_n={rt.get('retrieval.semantic_top_n')}, "
          f"candidate_top_n={rt.get('retrieval.candidate_top_n')}, "
          f"rerank_top_k={rt.get('retrieval.rerank_top_k')}, "
          f"rerank_threshold={rt.get('retrieval.rerank_threshold')}")

    # 裁判范围：--judge 全量；--ci 抽样；默认不跑
    judge_ids: set[str] | None = None
    if args.judge:
        judge_ids = {it["id"] for it in answerable}
    elif args.ci:
        sampled = sample_answerable(items, CI_JUDGE_SAMPLE)
        judge_ids = {it["id"] for it in sampled}
        print(f"生成段抽样（确定性，按章节轮转）：{sorted(judge_ids)}")

    install_api_caches()
    t0 = time.time()

    # 1) 检索层主评测
    print("\n--- 检索层评测（真实链路）---")
    retrieval = await run_retrieval_pass(items)
    retrieval_metrics = aggregate_retrieval(retrieval["per_item"])
    for name, m in retrieval_metrics.items():
        print(f"  {name}: recall@4={m['recall@4']} recall@8={m['recall@8']} "
              f"mrr={m['mrr']} ndcg@4={m['ndcg@4']}")

    # 2) rerank 阈值分析
    print("\n--- rerank 分数分布与阈值分析 ---")
    analysis = M.threshold_analysis(retrieval["rerank_records"], CANDIDATE_THRESHOLDS)
    print(f"  候选总数={analysis['total_candidates']} "
          f"相关={analysis['relevant_count']} 不相关={analysis['irrelevant_count']}")
    print(f"  全部分位数: {analysis['all_scores']}")
    print(f"  相关分数分位数: {analysis['relevant_scores']}")
    print(f"  不相关分数分位数: {analysis['irrelevant_scores']}")
    for row in analysis["per_threshold"]:
        print(f"  阈值 {row['threshold']}: 砍掉相关 {row['relevant_below']} 条 / "
              f"放过不相关 {row['irrelevant_kept']} 条 / 保留相关 {row['relevant_kept']} 条")

    # 3) 回答层（+按需裁判）
    answer_layer = None
    judge_metrics = None
    if not retrieval_only:
        print("\n--- 回答层评测（真实 DeepSeek）---")
        answer_layer = await run_answer_pass(items, judge_ids=judge_ids)
        m = answer_layer["metrics"]
        print(f"  answerable 准确率={m['answer_accuracy']}（含拒答误判） "
              f"unanswerable 拒答正确率={m['refusal_accuracy']} "
              f"answerable 误拒率={m['answerable_refused_rate']} "
              f"unanswerable 误答率={m['unanswerable_answered_rate']}")
        print(f"  代码层 answerability 预判拦截 {answer_layer['precheck_blocked']} 条（未进 LLM）")
        print(f"  ambiguous 行为：{answer_layer['ambiguous_breakdown']['refused']}"
              f"/{answer_layer['ambiguous_breakdown']['total']} 拒答")

        if judge_ids:
            verdicts = answer_layer["judge_verdicts"]
            judge_metrics = M.aggregate_judge_metrics(verdicts)
            print("\n--- 生成段裁判（LLM-as-a-Judge）---")
            print(f"  已评 {judge_metrics['judged_count']} 条"
                  f"（失败 {judge_metrics['judge_error_count']} 条），"
                  f"忠实度均值={judge_metrics['faithfulness_mean']} "
                  f"分布={judge_metrics['faithfulness_distribution']}")
            print(f"  切题度均值={judge_metrics['answer_relevance_mean']} "
                  f"分布={judge_metrics['answer_relevance_distribution']}")
            for v in verdicts:
                if v.get("judge_error"):
                    print(f"    [{v['id']}] 裁判失败：{v.get('error', '')[:80]}")
                else:
                    print(f"    [{v['id']}] 忠实度={v['faithfulness']} 切题度={v['answer_relevance']}"
                          f" | {v['reason'][:60]}")

    # 4) 参数网格（非 CI）
    grid_rows = None
    if args.grid:
        print("\n--- 参数网格（semantic_top_n × rerank_threshold，仅检索层）---")
        grid_rows = await run_grid(items)
        for row in grid_rows:
            print(f"  semantic_top_n={row['semantic_top_n']} "
                  f"RRF recall@8={row['rrf_recall@8']} RRF mrr={row['rrf_mrr']}")
            for th in row["per_threshold"]:
                print(f"    threshold={th['rerank_threshold']}: rerank recall@4={th['rerank_recall@4']} "
                      f"mrr={th['rerank_mrr']} 平均保留={th['avg_kept']}条")

    elapsed = time.time() - t0
    print(f"\n=== 完成，耗时 {elapsed:.1f}s，"
          f"API 调用：embedding {_api_stats['embed']} 次 / rerank {_api_stats['rerank']} 次 / "
          f"LLM {_api_stats['llm']} 次 ===")

    # 5) CI 门禁判定
    gate_rows = None
    exit_code = 0
    if args.ci:
        if answer_layer is None or judge_metrics is None:
            print("[错误] CI 模式内部异常：回答层未执行")
            sys.exit(1)
        gate_metrics = {
            "retrieval.rerank_recall_at_4": retrieval_metrics["rerank后"]["recall@4"],
            "retrieval.rrf_recall_at_8": retrieval_metrics["RRF排序"]["recall@8"],
            "retrieval.rerank_mrr": retrieval_metrics["rerank后"]["mrr"],
            "answer.answer_accuracy": answer_layer["metrics"]["answer_accuracy"],
            "answer.refusal_accuracy": answer_layer["metrics"]["refusal_accuracy"],
            "judge.faithfulness": judge_metrics["faithfulness_mean"],
            "judge.answer_relevance": judge_metrics["answer_relevance_mean"],
        }
        thresholds = load_thresholds()
        print("\n--- CI 门禁（对比 thresholds.json）---")
        gate_rows = ci_gate_check(gate_metrics, thresholds)
        failures = print_gate(gate_rows)
        if judge_metrics["judge_error_count"] > 0:
            failures.append(f"裁判失败 {judge_metrics['judge_error_count']} 条，生成段门禁无法验证")
            print(f"  [FAIL] 生成段存在裁判失败条目，门禁视为未通过")
        if failures:
            print("\n[门禁未通过] 跌破项：")
            for f in failures:
                print(f"  - {f}")
            exit_code = 1
        else:
            print("\n[门禁通过] 全部指标不低于阈值下限")
            exit_code = 0

    # 6) 落盘明细
    RESULTS_PATH.mkdir(parents=True, exist_ok=True)
    prefix = "eval-ci-results" if args.ci else "eval-results"
    out = RESULTS_PATH / f"{prefix}-{date.today().isoformat()}.json"
    out.write_text(
        json.dumps(
            {
                "meta": {
                    "date": date.today().isoformat(),
                    "mode": mode,
                    "dataset_size": len(items),
                    "answerable": len(answerable),
                    "params": {k: rt.get(k) for k in [
                        "retrieval.semantic_top_n", "retrieval.candidate_top_n",
                        "retrieval.rerank_top_k", "retrieval.rerank_threshold"]},
                    "elapsed_s": round(elapsed, 1),
                    "api_calls": dict(_api_stats),
                    "skip_answer": retrieval_only,
                    "judge_ids": sorted(judge_ids) if judge_ids else [],
                    "grid_run": bool(grid_rows),
                },
                "retrieval_metrics": retrieval_metrics,
                "retrieval_per_item": retrieval["per_item"],
                "threshold_analysis": analysis,
                "answer_layer": answer_layer,
                "judge_metrics": judge_metrics,
                "gate": gate_rows,
                "grid": grid_rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"明细已落盘：{out}")
    sys.exit(exit_code)


if __name__ == "__main__":
    asyncio.run(main())
