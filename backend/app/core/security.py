"""鉴权工具：密码哈希（pbkdf2）+ 自签 HMAC-SHA256 Token + FastAPI 依赖。

全部基于 Python 标准库，零第三方依赖，符合项目精简风格。
"""

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request

from app.config import settings
from app.db.database import get_db

# pbkdf2 迭代次数：兼顾安全与启动性能
_PBKDF2_ITERATIONS = 100_000
_HASH_ALGO = "sha256"


def generate_salt() -> str:
    """生成 16 字节随机盐，hex 编码存储。"""
    return secrets.token_hex(16)


def hash_password(password: str, salt: str) -> str:
    """pbkdf2_hmac 哈希密码，返回 hex 字符串。"""
    dk = hashlib.pbkdf2_hmac(
        _HASH_ALGO, password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERATIONS
    )
    return dk.hex()


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    """校验密码，常量时间比较防时序攻击。"""
    actual = hash_password(password, salt)
    return hmac.compare_digest(actual, expected_hash)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def create_token(user_id: int, email: str) -> str:
    """签发 token：payload.exp 过期时间，sig = HMAC-SHA256(secret, payload)。"""
    exp = datetime.now(timezone.utc) + timedelta(days=settings.token_expire_days)
    payload = {
        "uid": user_id,
        "email": email,
        "exp": int(exp.timestamp()),
    }
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(
        settings.auth_secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256
    ).digest()
    sig_b64 = _b64url_encode(sig)
    return f"{payload_b64}.{sig_b64}"


def decode_token(token: str) -> dict | None:
    """校验并解析 token，失败返回 None。"""
    try:
        payload_b64, sig_b64 = token.split(".", 1)
    except ValueError:
        return None
    expected_sig = hmac.new(
        settings.auth_secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256
    ).digest()
    try:
        sig = _b64url_decode(sig_b64)
    except Exception:
        return None
    if not hmac.compare_digest(expected_sig, sig):
        return None
    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception:
        return None
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < datetime.now(timezone.utc).timestamp():
        return None
    return payload


def get_current_user(request: Request) -> dict:
    """FastAPI 依赖：从 Authorization: Bearer <token> 解析当前登录用户。

    角色从 DB 实时读取（token 里不存角色），保证改角色后旧 token 的权限立即跟着变。

    用法：def handler(user: dict = Depends(get_current_user)) -> ...
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "未登录或登录已过期")
    token = auth[len("Bearer "):].strip()
    payload = decode_token(token)
    if payload is None:
        raise HTTPException(401, "登录已过期，请重新登录")
    user_id = payload.get("uid")
    rows = get_db().query("SELECT id, email, role FROM users WHERE id=?", (user_id,))
    if not rows:
        raise HTTPException(401, "用户不存在")
    return dict(rows[0])


def log_security_event(user: dict, action: str, path: str, detail: str) -> None:
    """把一次安全事件写入 security_logs。失败只打印，不影响主流程。"""
    from app.db.database import now

    try:
        get_db().execute(
            "INSERT INTO security_logs (user_id, user_email, action, path, detail, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (user["id"], user["email"], action, path, detail, now()),
        )
    except Exception as e:  # pragma: no cover - 记日志失败不应中断请求
        print(f"[security] 写安全日志失败: {e}")


async def _require_admin_impl(request: Request, user: dict = Depends(get_current_user)) -> dict:
    """管理接口的门禁：仅管理员可用。越权访问被拦截时写入安全日志。"""
    if user.get("role") != "admin":
        log_security_event(
            user,
            "permission_denied",
            request.url.path,
            f"普通用户尝试访问管理接口（角色={user.get('role')}）",
        )
        raise HTTPException(403, "该操作需要管理员权限")
    return user


def require_admin():
    """FastAPI 依赖工厂：用法 user: dict = Depends(require_admin())。"""
    return _require_admin_impl


__all__ = [
    "generate_salt",
    "hash_password",
    "verify_password",
    "create_token",
    "decode_token",
    "get_current_user",
    "require_admin",
    "log_security_event",
]
