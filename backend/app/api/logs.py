"""问答全链路日志接口：列表 + 详情（仅管理员可用）。

- GET /api/logs?limit=50：全部用户的问答日志摘要（带 user_email 区分归属）
- GET /api/logs/{log_id}：单条日志详情，含全链路 trace 节点
"""

import json

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.security import require_admin
from app.db.database import get_db

router = APIRouter()

MAX_LIMIT = 50


@router.get("/logs")
def list_logs(
    limit: int = Query(MAX_LIMIT, ge=1, le=MAX_LIMIT),
    user: dict = Depends(require_admin()),
) -> list[dict]:
    rows = get_db().query(
        "SELECT l.id, l.user_id, u.email AS user_email, l.question, l.intent_name,"
        " l.total_ms, l.status, l.created_at"
        " FROM logs l JOIN users u ON u.id = l.user_id"
        " ORDER BY l.id DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in rows]


@router.get("/logs/{log_id}")
def get_log(
    log_id: int,
    user: dict = Depends(require_admin()),
) -> dict:
    rows = get_db().query(
        "SELECT id, user_id, question, intent_name, trace, total_ms, status, created_at"
        " FROM logs WHERE id=?",
        (log_id,),
    )
    if not rows:
        raise HTTPException(404, "日志不存在")
    row = dict(rows[0])
    # trace 是 JSON 字符串，反序列化为对象供前端直接渲染
    try:
        row["trace"] = json.loads(row["trace"])
    except (json.JSONDecodeError, TypeError):
        row["trace"] = []
    return row
