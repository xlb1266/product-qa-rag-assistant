"""鉴权接口：邮箱 + 密码注册/登录，获取当前登录用户信息。

- POST /api/auth/register：自助注册，新用户一律普通用户（最小权限），注册即登录
- POST /api/auth/login：校验邮箱格式 + 密码（6 位数字），返回 token
- GET  /api/auth/me：需鉴权，返回当前用户（前端刷新页面时验证 token 有效性）
"""

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from app.core.security import (
    create_token,
    generate_salt,
    get_current_user,
    hash_password,
)
from app.db.database import get_db, now

router = APIRouter()

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_PASSWORD_RE = re.compile(r"^\d{6}$")


class LoginRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        if not _EMAIL_RE.match(v):
            raise ValueError("邮箱格式不正确")
        return v

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if not _PASSWORD_RE.match(v):
            raise ValueError("密码必须为 6 位数字")
        return v


class LoginResponse(BaseModel):
    token: str
    user: dict


def _validate_credentials(email: str, password: str) -> None:
    """业务层统一校验，失败返回 400 + 清晰中文提示。"""
    if not _EMAIL_RE.match(email):
        raise HTTPException(400, "邮箱格式不正确")
    if not _PASSWORD_RE.match(password):
        raise HTTPException(400, "密码必须为 6 位数字")


@router.post("/auth/register")
def register(req: LoginRequest) -> LoginResponse:
    """自助注册：新用户一律普通用户（最小权限），升级管理员需管理员人工操作。"""
    _validate_credentials(req.email, req.password)

    db = get_db()
    rows = db.query("SELECT id FROM users WHERE email=?", (req.email,))
    if rows:
        raise HTTPException(400, "该邮箱已注册，请直接登录")

    salt = generate_salt()
    password_hash = hash_password(req.password, salt)
    cur = db.execute(
        "INSERT INTO users (email, password_hash, salt, role, created_at)"
        " VALUES (?, ?, ?, 'user', ?)",
        (req.email, password_hash, salt, now()),
    )
    user_id = cur.lastrowid
    token = create_token(user_id, req.email)
    return LoginResponse(
        token=token,
        user={"id": user_id, "email": req.email, "role": "user"},
    )


@router.post("/auth/login")
def login(req: LoginRequest) -> LoginResponse:
    _validate_credentials(req.email, req.password)

    rows = get_db().query(
        "SELECT id, email, role, password_hash, salt FROM users WHERE email=?",
        (req.email,),
    )
    if not rows:
        raise HTTPException(401, "邮箱或密码错误")
    user = dict(rows[0])

    from app.core.security import verify_password

    if not verify_password(req.password, user["salt"], user["password_hash"]):
        raise HTTPException(401, "邮箱或密码错误")

    token = create_token(user["id"], user["email"])
    return LoginResponse(
        token=token,
        user={"id": user["id"], "email": user["email"], "role": user["role"]},
    )


@router.get("/auth/me")
def me(user: dict = Depends(get_current_user)) -> dict:
    return {"id": user["id"], "email": user["email"], "role": user["role"]}
