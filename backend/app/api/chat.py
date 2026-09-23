"""会话问答接口：登录鉴权 -> 意图识别 -> 按意图分流 -> 消息落库 + 全链路日志落库。

- SUPPORT / FEEDBACK：意图识别 -> 知识库混合检索 -> 重排 -> 回答生成
- CHAT：意图识别 -> 闲聊回复

鉴权改造后：
- 历史消息不再由前端携带，后端按登录用户从 DB 拉取最近 MAX_HISTORY 条作为上下文。
- 用户消息与机器人回复都写入 messages 表，绑定 user_id，支持跨设备同步。
- 响应返回两条完整记录（含 DB 自增 id），前端用真实 id 替换临时消息，避免 id 冲突。

全链路日志（编排层埋点，零侵入核心函数）：
- 用 perf_counter 给每个阶段计时，构建 trace 节点（name/service/input/output/duration_ms/status）。
- 链路末尾写入 logs 表，响应带 log_id 供前端跳转详情。
"""

import json
import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.messages import _row_to_dict
from app.core.answer import AnswerResult, generate_answer
from app.core.embeddings import EmbeddingError
from app.core.intent import CHAT, IntentResult, detect_intent
from app.core.llm import LLMError, get_llm_client
from app.core.retrieval import RetrievalResult, retrieve
from app.core.runtime_config import get_runtime
from app.core.security import get_current_user
from app.db.database import get_db, now

router = APIRouter()

CHAT_SYSTEM_PROMPT = (
    "你是产品咨询智能客服助手。当前用户在和你闲聊，"
    "请友好、自然、简洁地回应，体现客服的亲和力；"
    "如果用户聊到与产品相关的话题，可以顺势引导他具体描述需求。回复控制在三句话以内。"
)


def _max_history() -> int:
    """传给意图识别/回答模型的对话历史条数（运行时参数，调试页可改热生效）。"""
    return int(get_runtime().get("chat.max_history"))


def _mock_on() -> bool:
    """调试 mock 开关：意图/回答/向量化/重排等第三方调用走模拟返回，不产生 API 费用。"""
    return bool(get_runtime().get("debug.mock_third_party"))


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, description="用户最新消息")


class IntentInfo(BaseModel):
    name: str
    confidence: float
    resolved_question: str
    reason: str


class SearchHitOut(BaseModel):
    chunk_id: int
    score: float


class RetrievedChunkOut(BaseModel):
    chunk_id: int
    document_id: int
    document_title: str
    seq: int
    heading_path: str
    content: str
    score: float
    semantic_score: float | None = None
    bm25_score: float | None = None
    rrf_score: float | None = None


class RetrievalOut(BaseModel):
    query: str
    semantic_hits: list[SearchHitOut]
    bm25_hits: list[SearchHitOut]
    fused_hits: list[SearchHitOut]
    rerank_used: bool
    chunks: list[RetrievedChunkOut]


class AnswerOut(BaseModel):
    useful: bool
    content: str
    # 支撑回答的知识条目编号（对应 retrieval.chunks 下标+1）；历史消息无此字段，判空兼容
    citations: list[int] = []


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    intent: IntentInfo | None = None
    answer: AnswerOut | None = None
    retrieval: RetrievalOut | None = None
    question: str | None = None
    created_at: str


class ChatResponse(BaseModel):
    reply: str
    intent: IntentInfo
    answer: AnswerOut | None = None
    retrieval: RetrievalOut | None = None
    user_message: MessageOut
    bot_message: MessageOut
    log_id: int


def _load_history(user_id: int) -> list[dict]:
    """从 DB 拉取该用户最近 max_history 条消息，按时间升序返回。"""
    rows = get_db().query(
        "SELECT role, content FROM messages WHERE user_id=?"
        " ORDER BY id DESC LIMIT ?",
        (user_id, _max_history()),
    )
    history = [{"role": r["role"], "content": r["content"]} for r in rows]
    history.reverse()
    return history


def _insert_message(
    user_id: int,
    role: str,
    content: str,
    *,
    intent: dict | None = None,
    answer: dict | None = None,
    retrieval: dict | None = None,
    question: str | None = None,
) -> dict:
    """写入一条消息并返回完整行（JSON 字段序列化存储）。"""
    db = get_db()
    cur = db.execute(
        "INSERT INTO messages"
        " (user_id, role, content, intent, answer, retrieval, question, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id,
            role,
            content,
            json.dumps(intent, ensure_ascii=False) if intent else None,
            json.dumps(answer, ensure_ascii=False) if answer else None,
            json.dumps(retrieval, ensure_ascii=False) if retrieval else None,
            question,
            now(),
        ),
    )
    msg_id = cur.lastrowid
    row = db.query(
        "SELECT id, role, content, intent, answer, retrieval, question, created_at"
        " FROM messages WHERE id=?",
        (msg_id,),
    )[0]
    return _row_to_dict(row)


def _make_node(
    name: str,
    service: str,
    input_data: Any,
    output_data: Any,
    duration_ms: int,
    status: str = "success",
    error: str | None = None,
) -> dict:
    """构建一个链路节点。error 为 None 时不写入该键。"""
    node = {
        "name": name,
        "service": service,
        "input": input_data,
        "output": output_data,
        "duration_ms": duration_ms,
        "status": status,
    }
    if error is not None:
        node["error"] = error
    return node


def _insert_log(
    user_id: int,
    question: str,
    intent_name: str,
    trace: list[dict],
    total_ms: int,
    status: str,
) -> int:
    """写入一条全链路日志，返回 log_id。"""
    db = get_db()
    cur = db.execute(
        "INSERT INTO logs (user_id, question, intent_name, trace, total_ms, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            user_id,
            question,
            intent_name,
            json.dumps(trace, ensure_ascii=False),
            total_ms,
            status,
            now(),
        ),
    )
    return cur.lastrowid  # type: ignore[return-value]


@router.post("/chat")
async def chat(
    req: ChatRequest,
    user: dict = Depends(get_current_user),
) -> ChatResponse:
    chain_start = time.perf_counter()
    trace: list[dict] = []
    chain_status = "success"

    # 历史不再由前端携带，按登录用户从 DB 拉取
    history = _load_history(user["id"])

    # ===== 节点1：意图识别（所有问答都有）=====
    intent_input = {"question": req.message, "history_count": len(history)}
    t0 = time.perf_counter()
    try:
        if _mock_on():
            # 调试 mock：不调 DeepSeek，构造模拟意图（SUPPORT 走检索链路便于观察）
            result = IntentResult(
                intent="SUPPORT",
                confidence=0.99,
                resolved_question=req.message,
                reason="[mock] 模拟意图识别结果（调试模式）",
            )
            intent_node = _make_node(
                "intent_recognition", "DeepSeek(mock)", intent_input,
                {
                    "intent": result.intent,
                    "confidence": result.confidence,
                    "resolved_question": result.resolved_question,
                    "reason": result.reason,
                    "mocked": True,
                },
                int((time.perf_counter() - t0) * 1000),
            )
        else:
            result = await detect_intent(history, req.message)
            intent_node = _make_node(
                "intent_recognition",
                "DeepSeek",
                intent_input,
                {
                    "intent": result.intent,
                    "confidence": result.confidence,
                    "resolved_question": result.resolved_question,
                    "reason": result.reason,
                },
                int((time.perf_counter() - t0) * 1000),
            )
        trace.append(intent_node)
    except LLMError as e:
        trace.append(
            _make_node(
                "intent_recognition", "DeepSeek", intent_input, None,
                int((time.perf_counter() - t0) * 1000), "error", f"意图识别失败：{e}",
            )
        )
        _insert_log(user["id"], req.message, "UNKNOWN", trace,
                    int((time.perf_counter() - chain_start) * 1000), "error")
        raise HTTPException(502, f"意图识别失败：{e}")

    intent_out = IntentInfo(
        name=result.intent,
        confidence=result.confidence,
        resolved_question=result.resolved_question,
        reason=result.reason,
    )

    if result.intent == CHAT:
        # ===== CHAT 分支：意图识别 -> 闲聊回复 =====
        chat_input = {"question": req.message, "history_count": len(history)}
        t0 = time.perf_counter()
        try:
            if _mock_on():
                reply = "[mock] 模拟闲聊回复（调试模式，未调用大模型）。"
                trace.append(
                    _make_node(
                        "chat_reply", "DeepSeek(mock)", chat_input,
                        {"reply": reply, "mocked": True},
                        int((time.perf_counter() - t0) * 1000),
                    )
                )
            else:
                reply = await get_llm_client().chat(
                    messages=[
                        {"role": "system", "content": CHAT_SYSTEM_PROMPT},
                        *history,
                        {"role": "user", "content": req.message},
                    ]
                )
                trace.append(
                    _make_node(
                        "chat_reply", "DeepSeek", chat_input, {"reply": reply},
                        int((time.perf_counter() - t0) * 1000),
                    )
                )
        except LLMError as e:
            trace.append(
                _make_node(
                    "chat_reply", "DeepSeek", chat_input, None,
                    int((time.perf_counter() - t0) * 1000), "error", f"生成回复失败：{e}",
                )
            )
            _insert_log(user["id"], req.message, result.intent, trace,
                        int((time.perf_counter() - chain_start) * 1000), "error")
            raise HTTPException(502, f"生成回复失败：{e}")

        # 落库消息
        user_msg = _insert_message(user["id"], "user", req.message)
        bot_msg = _insert_message(
            user["id"], "assistant", reply, intent=intent_out.model_dump()
        )
        log_id = _insert_log(
            user["id"], req.message, result.intent, trace,
            int((time.perf_counter() - chain_start) * 1000), chain_status,
        )
        return ChatResponse(
            reply=reply,
            intent=intent_out,
            user_message=MessageOut(**user_msg),
            bot_message=MessageOut(**bot_msg),
            log_id=log_id,
        )

    # ===== SUPPORT / FEEDBACK 分支：检索 -> 重排 -> 回答 =====

    # ----- 节点2：知识检索（语义+BM25+RRF，封装在 retrieve 内）-----
    retrieval_input = {"query": result.resolved_question}
    t0 = time.perf_counter()
    try:
        retrieval = await retrieve(result.resolved_question)
        retrieval_node = _make_node(
            "retrieval",
            "百炼embedding + FAISS + 本地BM25" + ("(mock)" if retrieval.mocked else ""),
            retrieval_input,
            {
                "semantic_hits": len(retrieval.semantic_hits),
                "bm25_hits": len(retrieval.bm25_hits),
                "fused_hits": len(retrieval.fused_hits),
                "candidate_count": len(retrieval.fused_hits),
                **({"mocked": True} if retrieval.mocked else {}),
            },
            int((time.perf_counter() - t0) * 1000),
        )
        trace.append(retrieval_node)
    except EmbeddingError as e:
        trace.append(
            _make_node(
                "retrieval", "百炼embedding + FAISS + 本地BM25", retrieval_input, None,
                int((time.perf_counter() - t0) * 1000), "error", f"语义检索失败：{e}",
            )
        )
        _insert_log(user["id"], req.message, result.intent, trace,
                    int((time.perf_counter() - chain_start) * 1000), "error")
        raise HTTPException(502, f"语义检索失败：{e}")
    except Exception as e:
        trace.append(
            _make_node(
                "retrieval", "百炼embedding + FAISS + 本地BM25", retrieval_input, None,
                int((time.perf_counter() - t0) * 1000), "error", f"知识库检索失败：{e}",
            )
        )
        _insert_log(user["id"], req.message, result.intent, trace,
                    int((time.perf_counter() - chain_start) * 1000), "error")
        raise HTTPException(500, f"知识库检索失败：{e}")

    # ----- 节点3：重排（封装在 retrieve 内部，编排层无法单独计时）-----
    # retrieve() 把检索+重排合成一步返回，重排耗时无法在编排层单独拆出，
    # 此节点如实记录：duration_ms 标 0 并在 output 注明，input/output 仍完整。
    rerank_output = {
        "rerank_used": retrieval.rerank_used,
        "results": [{"chunk_id": c.chunk_id, "score": c.score} for c in retrieval.chunks],
        "note": "重排在 retrieve 内部执行，耗时已含入 retrieval 节点" if retrieval.rerank_used
        else ("重排被 mock 跳过，使用 RRF/BM25 排序" if retrieval.mocked else "重排降级，使用 RRF 排序"),
    }
    if retrieval.mocked:
        rerank_output["mocked"] = True
    trace.append(
        _make_node(
            "rerank", "百炼 qwen3-rerank" + ("(mock)" if retrieval.mocked else ""),
            {"query": result.resolved_question, "documents_count": len(retrieval.fused_hits)},
            rerank_output, 0,
            "success" if retrieval.rerank_used else "degraded",
        )
    )

    # ----- 节点4：回答生成 -----
    answer_input = {
        "question": req.message,
        "history_count": len(history),
        "chunks_count": len(retrieval.chunks),
    }
    t0 = time.perf_counter()
    try:
        if _mock_on():
            # 调试 mock：不调 DeepSeek，基于本地检索结果拼一个模拟回答
            if retrieval.chunks:
                top = retrieval.chunks[0]
                answer = AnswerResult(
                    useful=True,
                    content=f"[mock] 模拟回答（调试模式）：最相关的知识来自《{top.document_title}》"
                    f"「{top.heading_path or top.content[:24]}」，BM25 分数 {top.bm25_score}。",
                    citations=[1],
                )
            else:
                answer = AnswerResult(useful=False, content="暂时不能回答这个问题")
            trace.append(
                _make_node(
                    "answer_generation", "DeepSeek(mock)", answer_input,
                    {
                        "useful": answer.useful,
                        "content": answer.content,
                        "citations": answer.citations,
                        "mocked": True,
                    },
                    int((time.perf_counter() - t0) * 1000),
                )
            )
        else:
            answer = await generate_answer(history, req.message, retrieval.chunks)
            trace.append(
                _make_node(
                    "answer_generation", "DeepSeek", answer_input,
                    {
                        "useful": answer.useful,
                        "content": answer.content,
                        "citations": answer.citations,
                    },
                    int((time.perf_counter() - t0) * 1000),
                )
            )
    except LLMError as e:
        trace.append(
            _make_node(
                "answer_generation", "DeepSeek", answer_input, None,
                int((time.perf_counter() - t0) * 1000), "error", f"回答生成失败：{e}",
            )
        )
        _insert_log(user["id"], req.message, result.intent, trace,
                    int((time.perf_counter() - chain_start) * 1000), "error")
        raise HTTPException(502, f"回答生成失败：{e}")

    answer_out = AnswerOut(
        useful=answer.useful, content=answer.content, citations=answer.citations
    )
    retrieval_out = _to_retrieval_out(retrieval)

    user_msg = _insert_message(user["id"], "user", req.message)
    bot_msg = _insert_message(
        user["id"],
        "assistant",
        answer.content,
        intent=intent_out.model_dump(),
        answer=answer_out.model_dump(),
        retrieval=retrieval_out.model_dump(),
        question=req.message,
    )
    log_id = _insert_log(
        user["id"], req.message, result.intent, trace,
        int((time.perf_counter() - chain_start) * 1000), chain_status,
    )
    return ChatResponse(
        reply=answer.content,
        answer=answer_out,
        intent=intent_out,
        retrieval=retrieval_out,
        user_message=MessageOut(**user_msg),
        bot_message=MessageOut(**bot_msg),
        log_id=log_id,
    )


def _to_retrieval_out(r: RetrievalResult) -> RetrievalOut:
    return RetrievalOut(
        query=r.query,
        semantic_hits=[SearchHitOut(**h.__dict__) for h in r.semantic_hits],
        bm25_hits=[SearchHitOut(**h.__dict__) for h in r.bm25_hits],
        fused_hits=[SearchHitOut(**h.__dict__) for h in r.fused_hits],
        rerank_used=r.rerank_used,
        chunks=[RetrievedChunkOut(**c.__dict__) for c in r.chunks],
    )


# 延迟导入避免循环依赖：embeddings 被 retrieval 引用，错误类型在此处使用
from app.core.embeddings import EmbeddingError  # noqa: E402
