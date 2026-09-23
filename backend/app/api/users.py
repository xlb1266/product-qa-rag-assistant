"""用户管理接口（仅管理员）：用户列表、角色调整（留痕 + 二次验证）、安全事件。

- GET  /api/users：全部用户列表（不含密码材料）
- PUT  /api/users/{id}/role：调整角色。关键操作，要求携带管理员当前密码二次验证；
       写 role_changes 留痕（谁在什么时间把谁从什么角色改成了什么角色）
- GET  /api/users/role-changes：角色变更历史
- GET  /api/security-logs：最近的安全事件（越权拦截记录）
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from app.core.security import require_admin, verify_password
from app.db.database import get_db, now

router = APIRouter()

_VALID_ROLES = ("user", "admin")


class RoleChangeRequest(BaseModel):
    role: str
    # 关键操作二次验证：改别人权限前需确认操作者身份
    admin_password: str

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in _VALID_ROLES:
            raise ValueError(f"角色只能是 {'/'.join(_VALID_ROLES)}")
        return v


def _admin_row(user_id: int) -> dict:
    rows = get_db().query(
        "SELECT id, email, password_hash, salt FROM users WHERE id=?", (user_id,)
    )
    if not rows:
        raise HTTPException(404, "用户不存在")
    return dict(rows[0])


@router.get("/users")
def list_users(user: dict = Depends(require_admin())) -> list[dict]:
    rows = get_db().query(
        "SELECT id, email, role, created_at FROM users ORDER BY id"
    )
    return [dict(r) for r in rows]


@router.put("/users/{user_id}/role")
def change_role(
    user_id: int,
    req: RoleChangeRequest,
    user: dict = Depends(require_admin()),
) -> dict:
    # 1) 二次验证：操作者必须携带自己的当前密码
    operator = _admin_row(user["id"])
    if not verify_password(req.admin_password, operator["salt"], operator["password_hash"]):
        raise HTTPException(403, "管理员密码不正确，已拒绝本次角色变更")

    # 2) 不允许改自己的角色：防止误操作把唯一管理员降级锁死系统
    if user_id == user["id"]:
        raise HTTPException(400, "不能修改自己的角色")

    target = _admin_row(user_id)
    old_role = get_db().query("SELECT role FROM users WHERE id=?", (user_id,))[0]["role"]
    if old_role == req.role:
        raise HTTPException(400, f"用户当前已是该角色（{req.role}），无需变更")

    # 3) 安全限制：管理员不能降级其他管理员，防止管理员间互相剥夺权限
    if old_role == "admin" and req.role == "user":
        raise HTTPException(
            403,
            "不能降级其他管理员。如需调整其角色，请先由该管理员自行降级或联系系统管理员处理。",
        )

    # 3) 变更 + 留痕（同一把锁内完成，见 Database.execute）
    db = get_db()
    db.execute("UPDATE users SET role=? WHERE id=?", (req.role, user_id))
    db.execute(
        "INSERT INTO role_changes"
        " (operator_id, target_user_id, target_email, old_role, new_role, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (user["id"], user_id, target["email"], old_role, req.role, now()),
    )
    return {
        "ok": True,
        "user_id": user_id,
        "email": target["email"],
        "old_role": old_role,
        "new_role": req.role,
    }


@router.get("/users/role-changes")
def list_role_changes(user: dict = Depends(require_admin())) -> list[dict]:
    rows = get_db().query(
        "SELECT rc.id, rc.target_email, rc.old_role, rc.new_role, rc.created_at,"
        " op.email AS operator_email"
        " FROM role_changes rc JOIN users op ON op.id = rc.operator_id"
        " ORDER BY rc.id DESC LIMIT 100"
    )
    return [dict(r) for r in rows]


@router.get("/security-logs")
def list_security_logs(user: dict = Depends(require_admin())) -> list[dict]:
    rows = get_db().query(
        "SELECT id, user_email, action, path, detail, created_at"
        " FROM security_logs ORDER BY id DESC LIMIT 100"
    )
    return [dict(r) for r in rows]
