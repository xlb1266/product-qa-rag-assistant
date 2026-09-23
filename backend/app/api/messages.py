"""会话历史接口：按登录用户拉取最近消息，实现跨设备同步。

- GET /api/messages?limit=50：返回当前用户最近的消息（升序），单次最多 50 条
"""

import json

from fastapi import APIRouter, Depends, Query

from app.core.security import get_current_user
from app.db.database import get_db

router = APIRouter()

# 需求限定：单次最多携带最近 50 条记录
MAX_LIMIT = 50


def _row_to_dict(row) -> dict:
    """把 DB 行转成前端消息对象，JSON 字段反序列化。"""
    d = dict(row)
    for key in ("intent", "answer", "retrieval"):
        if d.get(key):
            try:
                d[key] = json.loads(d[key])
            except (json.JSONDecodeError, TypeError):
                d[key] = None
    return d


@router.get("/messages")
def list_messages(
    limit: int = Query(MAX_LIMIT, ge=1, le=MAX_LIMIT),
    user: dict = Depends(get_current_user),
) -> list[dict]:
    # 按 id DESC 取最近 N 条，再反转为时间升序，方便前端顺序渲染
    rows = get_db().query(
        "SELECT id, role, content, intent, answer, retrieval, question, created_at"
        " FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user["id"], limit),
    )
    items = [_row_to_dict(r) for r in rows]
    items.reverse()
    return items


@router.delete("/messages")
def clear_messages(user: dict = Depends(get_current_user)) -> dict:
    """清空当前用户的全部会话消息。

    只删 messages 表；logs 表（问答全链路日志）保留，日志页仍可追溯历史问答。
    清空后 /api/chat 的对话历史从空开始，多轮上下文重新建立。
    """
    db = get_db()
    cur = db.execute(
        "DELETE FROM messages WHERE user_id=?", (user["id"],)
    )
    return {"ok": True, "deleted": cur.rowcount}
