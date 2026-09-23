"""数据飞轮接口：用户反馈收集 + 人工审核 + 高质量问答库。

- POST /api/feedback：用户对 bot 回复点赞/点踩（只能评自己会话里的消息）。
  点赞时自动把该问答收集进审核池（先脱敏）；收集失败只记日志，不影响点赞本身。
- GET  /api/reviews?status=：审核池列表（仅管理员），三合一展示待审核数据
- PUT  /api/reviews/{id}：审核结论 approved / rejected / revised。
  通过与修改后通过会把 Q/A 生成 Markdown 走知识库入库管线（分块→向量化→FAISS），
  下次同类问题即可命中；同时写入 qa_library 记录来源与批次。
- GET  /api/qa-library：高质量问答库列表（仅管理员）

数据用途与保存期限（写在审核页上）：仅用于改进问答质量，脱敏后保存，不做任何对外共享。
"""

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator

from app.core.sanitizer import sanitize_text
from app.core.security import get_current_user, require_admin
from app.db.database import get_db, now

router = APIRouter()


class FeedbackRequest(BaseModel):
    message_id: int
    rating: int  # 1 点赞 / -1 点踩

    @field_validator("rating")
    @classmethod
    def validate_rating(cls, v: int) -> int:
        if v not in (1, -1):
            raise ValueError("rating 只能是 1（点赞）或 -1（点踩）")
        return v


class ReviewDecisionRequest(BaseModel):
    decision: str  # approved / rejected / revised
    note: str = ""
    revised_question: str | None = None
    revised_answer: str | None = None

    @field_validator("decision")
    @classmethod
    def validate_decision(cls, v: str) -> str:
        if v not in ("approved", "rejected", "revised"):
            raise ValueError("decision 只能是 approved / rejected / revised")
        return v


@router.post("/feedback")
def submit_feedback(
    req: FeedbackRequest,
    user: dict = Depends(get_current_user),
) -> dict:
    db = get_db()
    rows = db.query(
        "SELECT id, user_id, role, content, answer, question FROM messages WHERE id=?",
        (req.message_id,),
    )
    if not rows:
        raise HTTPException(404, "消息不存在")
    msg = dict(rows[0])
    if msg["user_id"] != user["id"]:
        raise HTTPException(403, "只能评价自己会话中的消息")
    if msg["role"] != "assistant":
        raise HTTPException(400, "只能评价客服回复消息")

    # 1) 记录反馈（幂等：重复评价覆盖更新）
    db.execute(
        "UPDATE messages SET feedback=?, feedback_at=? WHERE id=?",
        (req.rating, now(), req.message_id),
    )

    # 2) 点赞 -> 自动收集进审核池（不阻塞主流程，失败只记日志）
    collected = False
    if req.rating == 1:
        try:
            collected = _collect_to_review_queue(user, msg)
        except Exception as e:
            print(f"[flywheel] 收集进审核池失败（不影响反馈记录）: message_id={req.message_id}, err={e}")

    return {"ok": True, "rating": req.rating, "collected": collected}


def _collect_to_review_queue(user: dict, msg: dict) -> bool:
    """把点赞问答（脱敏后）写入待审核池。已入池过则跳过（幂等）。"""
    db = get_db()
    exists = db.query(
        "SELECT id FROM review_queue WHERE message_id=?", (msg["id"],)
    )
    if exists:
        return False

    # question 只在 SUPPORT/FEEDBACK 分支落库；CHAT 闲聊没有 question 且无 answer JSON，
    # _answer_content 会返回空 -> 不收集（闲聊不作为知识沉淀，"不该收的不收"）
    question = msg.get("question") or ""
    answer_text = _answer_content(msg)
    if not question or not answer_text:
        # 无法还原问题或回答为空（如兜底回复）：不收集
        print(f"[flywheel] 消息 {msg['id']} 缺少问题或有效回答，跳过收集")
        return False

    # 入库前脱敏：问题与回答分别脱敏，记录发现的敏感类型
    q_safe, q_types = sanitize_text(question)
    a_safe, a_types = sanitize_text(answer_text)
    sanitized_types = sorted(set(q_types + a_types))

    # 关联的请求编号：取该用户 assistant 消息前的最近一条 user 消息无法直接拿到 log_id，
    # messages 表没有存 log_id，这里以消息自身时间就近关联（问答成对落库，id 相邻）
    log_id = _nearby_log_id(msg["user_id"], msg["id"])

    db.execute(
        "INSERT INTO review_queue"
        " (message_id, user_id, user_email, question, answer, log_id,"
        "  sanitized_types, status, feedback, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 1, ?)",
        (
            msg["id"], user["id"], user["email"], q_safe, a_safe, log_id,
            json.dumps(sanitized_types, ensure_ascii=False), now(),
        ),
    )
    return True


def _answer_content(msg: dict) -> str:
    """有效回答 = answer.useful=true 的 content；useful=false 的兜底回复不进池。"""
    raw = msg.get("answer")
    if not raw:
        # CHAT 闲聊回复没有 answer JSON，闲聊不作为知识沉淀
        return ""
    try:
        answer = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not answer.get("useful"):
        return ""
    return str(answer.get("content") or "")


def _nearby_log_id(user_id: int, message_id: int) -> int | None:
    """问答成对落库（user 消息 id 与 assistant 消息 id 相邻），

    用 assistant 消息前紧邻的 user 消息内容在 logs 表里反查请求编号，便于审核页跳转溯源。
    """
    db = get_db()
    rows = db.query(
        "SELECT content FROM messages WHERE user_id=? AND role='user' AND id<?"
        " ORDER BY id DESC LIMIT 1",
        (user_id, message_id),
    )
    if not rows:
        return None
    logs = db.query(
        "SELECT id FROM logs WHERE user_id=? AND question=? ORDER BY id DESC LIMIT 1",
        (user_id, rows[0]["content"]),
    )
    return logs[0]["id"] if logs else None


@router.get("/reviews")
def list_reviews(
    status: str = Query("pending", pattern="^(pending|approved|rejected|revised|all)$"),
    user: dict = Depends(require_admin()),
) -> list[dict]:
    if status == "all":
        rows = get_db().query(
            "SELECT id, message_id, user_email, question, answer, log_id,"
            " sanitized_types, status, reviewer_email, review_note,"
            " revised_question, revised_answer, feedback, created_at, reviewed_at"
            " FROM review_queue ORDER BY id DESC LIMIT 200"
        )
    else:
        rows = get_db().query(
            "SELECT id, message_id, user_email, question, answer, log_id,"
            " sanitized_types, status, reviewer_email, review_note,"
            " revised_question, revised_answer, feedback, created_at, reviewed_at"
            " FROM review_queue WHERE status=? ORDER BY id DESC LIMIT 200",
            (status,),
        )
    items = []
    for r in rows:
        d = dict(r)
        try:
            d["sanitized_types"] = json.loads(d.get("sanitized_types") or "[]")
        except json.JSONDecodeError:
            d["sanitized_types"] = []
        items.append(d)
    return items


@router.put("/reviews/{review_id}")
async def decide_review(
    review_id: int,
    req: ReviewDecisionRequest,
    user: dict = Depends(require_admin()),
) -> dict:
    db = get_db()
    rows = db.query("SELECT * FROM review_queue WHERE id=?", (review_id,))
    if not rows:
        raise HTTPException(404, "审核记录不存在")
    review = dict(rows[0])
    if review["status"] != "pending":
        raise HTTPException(400, f"该记录已审核过（当前状态 {review['status']}），不能重复审核")

    final_question = review["question"]
    final_answer = review["answer"]
    if req.decision == "revised":
        # 修改后通过：必须提供修订文本，修订版同样过一遍脱敏（防止审核人粘贴带敏感信息的修订）
        if not (req.revised_question and req.revised_answer):
            raise HTTPException(400, "修改后通过需同时提供修订后的问题与回答")
        final_question, _ = sanitize_text(req.revised_question)
        final_answer, _ = sanitize_text(req.revised_answer)

    document_id = None
    qa_id = None
    if req.decision in ("approved", "revised"):
        # 反哺：Q/A 生成 Markdown 入知识库（分块 -> 向量化 -> FAISS），下次同类问题可命中
        doc = await _fountain_to_knowledge(review_id, final_question, final_answer)
        document_id = doc["id"]
        cur = db.execute(
            "INSERT INTO qa_library (review_id, question, answer, source, document_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                review_id, final_question, final_answer,
                f"用户点赞审核（贡献者 {review['user_email']}，审核人 {user['email']}）",
                document_id, now(),
            ),
        )
        qa_id = cur.lastrowid

    db.execute(
        "UPDATE review_queue SET status=?, reviewer_email=?, review_note=?,"
        " revised_question=?, revised_answer=?, reviewed_at=? WHERE id=?",
        (
            req.decision, user["email"], req.note,
            req.revised_question if req.decision == "revised" else None,
            req.revised_answer if req.decision == "revised" else None,
            now(), review_id,
        ),
    )
    return {
        "ok": True,
        "review_id": review_id,
        "decision": req.decision,
        "document_id": document_id,
        "qa_library_id": qa_id,
        "message": "已通过并将问答沉淀进知识库" if document_id else "已标记不通过，不会进入高质量库",
    }


async def _fountain_to_knowledge(review_id: int, question: str, answer: str) -> dict:
    """把高质量 Q/A 生成 Markdown 文档并走知识库入库管线。"""
    from app.api.documents import process_document_text

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"飞轮问答-{stamp}-r{review_id}.md"
    markdown = (
        "# 高质量问答沉淀\n\n"
        "> 来源：用户点赞 + 人工审核通过（数据飞轮），用于提升同类问题的检索命中率。\n\n"
        f"## Q：{question}\n\n{answer}\n"
    )
    return await process_document_text(filename, markdown)


@router.get("/qa-library")
def list_qa_library(user: dict = Depends(require_admin())) -> list[dict]:
    rows = get_db().query(
        "SELECT id, review_id, question, answer, source, document_id, created_at"
        " FROM qa_library ORDER BY id DESC LIMIT 200"
    )
    return [dict(r) for r in rows]
