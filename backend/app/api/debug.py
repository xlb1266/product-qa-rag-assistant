"""调试接口（仅管理员）：运行时参数管理（热生效+留痕+回滚）、请求反查、链路重放。

- GET  /api/debug/config：参数定义 + 当前值 + 只读服务信息（密钥打码）
- PUT  /api/debug/config：批量更新参数（先整体校验，不合法整条拒绝）
- POST /api/debug/config/{key}/rollback：回滚到最近一次修改前的值
- GET  /api/debug/config/changes：参数修改历史
- POST /api/debug/replay：按日志记录的原始输入重跑问答链路（不落库、不产生费用可选）

与日志功能共用同一套请求编号（logs.id）和节点定义（见 docs/pipeline-nodes.md）。
"""

import json
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.chat import _make_node
from app.config import settings
from app.core.intent import CHAT, IntentResult, detect_intent
from app.core.llm import LLMError, get_llm_client
from app.core.retrieval import retrieve
from app.core.runtime_config import ValidationError, get_runtime
from app.core.security import require_admin
from app.db.database import get_db

router = APIRouter()


def _mask_key(key: str) -> str:
    """密钥打码：只保留前 3 位与后 4 位。"""
    if not key:
        return "(未配置)"
    if len(key) <= 8:
        return "****"
    return key[:3] + "****" + key[-4:]


class ConfigUpdateRequest(BaseModel):
    updates: dict[str, object]


class ReplayRequest(BaseModel):
    log_id: int


@router.get("/debug/config")
def get_config(user: dict = Depends(require_admin())) -> dict:
    rt = get_runtime()
    params = []
    for d in rt.defs():
        params.append(
            {
                "key": d.key,
                "label": d.label,
                "type": d.type,
                "group": d.group,
                "default": d.default,
                "min": d.min,
                "max": d.max,
                "current": rt.get(d.key),
            }
        )
    # 只读服务信息：第三方地址与模型名可看，密钥一律打码
    info = {
        "deepseek_base_url": settings.deepseek_base_url,
        "deepseek_model": settings.deepseek_model,
        "deepseek_api_key": _mask_key(settings.deepseek_api_key),
        "dashscope_base_url": settings.dashscope_base_url,
        "embedding_model": settings.embedding_model,
        "dashscope_api_key": _mask_key(settings.dashscope_api_key),
        "rerank_model": settings.rerank_model,
    }
    return {"params": params, "services": info}


@router.put("/debug/config")
def update_config(
    req: ConfigUpdateRequest,
    user: dict = Depends(require_admin()),
) -> dict:
    try:
        return get_runtime().set_many(req.updates, operator=user["email"])
    except ValidationError as e:
        raise HTTPException(400, str(e))


@router.post("/debug/config/{key}/rollback")
def rollback_config(key: str, user: dict = Depends(require_admin())) -> dict:
    try:
        return get_runtime().rollback(key, operator=user["email"])
    except ValidationError as e:
        raise HTTPException(400, str(e))


@router.get("/debug/config/changes")
def config_changes(user: dict = Depends(require_admin())) -> list[dict]:
    rows = get_db().query(
        "SELECT id, key, old_value, new_value, updated_by, created_at"
        " FROM config_changes ORDER BY id DESC LIMIT 100"
    )
    items = []
    for r in rows:
        d = dict(r)
        try:
            d["old_value"] = json.loads(d["old_value"])
            d["new_value"] = json.loads(d["new_value"])
        except json.JSONDecodeError:
            pass
        items.append(d)
    return items


@router.post("/debug/replay")
async def replay(req: ReplayRequest, user: dict = Depends(require_admin())) -> dict:
    """把某次问答的原始输入重新跑一遍，返回重放链路（不写消息、不写日志）。

    是否产生第三方费用由 debug.mock_third_party 开关决定：
    开启时意图/回答/向量化/重排全部走模拟返回，节点 output 标注 mocked=true。
    """
    rows = get_db().query(
        "SELECT id, user_id, question, intent_name FROM logs WHERE id=?",
        (req.log_id,),
    )
    if not rows:
        raise HTTPException(404, "日志不存在")
    row = dict(rows[0])

    rt = get_runtime()
    mocked = bool(rt.get("debug.mock_third_party"))
    question = row["question"]
    trace: list[dict] = []
    start = time.perf_counter()

    # 节点1：意图识别
    t0 = time.perf_counter()
    if mocked:
        result = IntentResult(
            intent="SUPPORT", confidence=0.99, resolved_question=question,
            reason="[mock] 模拟意图识别结果（重放调试）",
        )
        trace.append(_make_node(
            "intent_recognition", "DeepSeek(mock)", {"question": question},
            {**result.__dict__, "mocked": True},
            int((time.perf_counter() - t0) * 1000),
        ))
    else:
        try:
            result = await detect_intent([], question)
            trace.append(_make_node(
                "intent_recognition", "DeepSeek", {"question": question},
                result.__dict__, int((time.perf_counter() - t0) * 1000),
            ))
        except LLMError as e:
            trace.append(_make_node(
                "intent_recognition", "DeepSeek", {"question": question}, None,
                int((time.perf_counter() - t0) * 1000), "error", f"意图识别失败：{e}",
            ))
            return _replay_result(row, trace, start, mocked, error=str(e))

    # CHAT 分支：闲聊回复
    if result.intent == CHAT:
        t0 = time.perf_counter()
        if mocked:
            reply = "[mock] 模拟闲聊回复（重放调试，未调用大模型）。"
            trace.append(_make_node(
                "chat_reply", "DeepSeek(mock)", {"question": question},
                {"reply": reply, "mocked": True},
                int((time.perf_counter() - t0) * 1000),
            ))
        else:
            try:
                reply = await get_llm_client().chat(
                    messages=[{"role": "user", "content": question}]
                )
                trace.append(_make_node(
                    "chat_reply", "DeepSeek", {"question": question},
                    {"reply": reply}, int((time.perf_counter() - t0) * 1000),
                ))
            except LLMError as e:
                trace.append(_make_node(
                    "chat_reply", "DeepSeek", {"question": question}, None,
                    int((time.perf_counter() - t0) * 1000), "error", f"生成回复失败：{e}",
                ))
                return _replay_result(row, trace, start, mocked, error=str(e))
        return _replay_result(row, trace, start, mocked, reply=reply)

    # SUPPORT / FEEDBACK 分支：检索 -> 重排（retrieve 内部）-> 回答
    t0 = time.perf_counter()
    try:
        retrieval = await retrieve(result.resolved_question)
    except Exception as e:
        trace.append(_make_node(
            "retrieval", "百炼embedding + FAISS + 本地BM25",
            {"query": result.resolved_question}, None,
            int((time.perf_counter() - t0) * 1000), "error", f"检索失败：{e}",
        ))
        return _replay_result(row, trace, start, mocked, error=str(e))

    trace.append(_make_node(
        "retrieval",
        "百炼embedding + FAISS + 本地BM25" + ("(mock)" if retrieval.mocked else ""),
        {"query": result.resolved_question},
        {
            "semantic_hits": len(retrieval.semantic_hits),
            "bm25_hits": len(retrieval.bm25_hits),
            "fused_hits": len(retrieval.fused_hits),
            **({"mocked": True} if retrieval.mocked else {}),
        },
        int((time.perf_counter() - t0) * 1000),
    ))
    trace.append(_make_node(
        "rerank", "百炼 qwen3-rerank" + ("(mock)" if retrieval.mocked else ""),
        {"query": result.resolved_question, "documents_count": len(retrieval.fused_hits)},
        {
            "rerank_used": retrieval.rerank_used,
            "results": [{"chunk_id": c.chunk_id, "score": c.score} for c in retrieval.chunks],
            "note": "重排被 mock 跳过，使用 RRF/BM25 排序" if retrieval.mocked
            else "重排在 retrieve 内部执行，耗时已含入 retrieval 节点",
            **({"mocked": True} if retrieval.mocked else {}),
        },
        0, "success" if retrieval.rerank_used else "degraded",
    ))

    # 回答生成
    from app.core.answer import AnswerResult, generate_answer

    t0 = time.perf_counter()
    if mocked:
        if retrieval.chunks:
            top = retrieval.chunks[0]
            answer = AnswerResult(
                useful=True,
                content=f"[mock] 模拟回答（重放调试）：最相关片段来自《{top.document_title}》"
                f"「{top.heading_path or top.content[:24]}」。",
            )
        else:
            answer = AnswerResult(useful=False, content="暂时不能回答这个问题")
        trace.append(_make_node(
            "answer_generation", "DeepSeek(mock)",
            {"question": question, "chunks_count": len(retrieval.chunks)},
            {"useful": answer.useful, "content": answer.content, "mocked": True},
            int((time.perf_counter() - t0) * 1000),
        ))
    else:
        try:
            answer = await generate_answer([], question, retrieval.chunks)
            trace.append(_make_node(
                "answer_generation", "DeepSeek",
                {"question": question, "chunks_count": len(retrieval.chunks)},
                {"useful": answer.useful, "content": answer.content},
                int((time.perf_counter() - t0) * 1000),
            ))
        except LLMError as e:
            trace.append(_make_node(
                "answer_generation", "DeepSeek",
                {"question": question, "chunks_count": len(retrieval.chunks)}, None,
                int((time.perf_counter() - t0) * 1000), "error", f"回答生成失败：{e}",
            ))
            return _replay_result(row, trace, start, mocked, error=str(e))

    return _replay_result(
        row, trace, start, mocked, reply=answer.content, useful=answer.useful
    )


def _replay_result(
    row: dict, trace: list[dict], start: float, mocked: bool,
    reply: str | None = None, useful: bool | None = None, error: str | None = None,
) -> dict:
    return {
        "replayed_log_id": row["id"],
        "original_user_id": row["user_id"],
        "original_intent": row["intent_name"],
        "question": row["question"],
        "reply": reply,
        "useful": useful,
        "mock_used": mocked,
        "total_ms": int((time.perf_counter() - start) * 1000),
        "status": "error" if error else "success",
        "error": error,
        "trace": trace,
    }
