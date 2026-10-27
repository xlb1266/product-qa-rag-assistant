"""SQLite 数据库访问层。

八张表：
- documents:     上传的文档元信息
- chunks:        文档分块，其自增 id 同时作为 FAISS 中的向量 ID（唯一且不回退）
- users:         登录用户（邮箱 + 密码 + 角色），初始化时内置 admin / user 两个测试账号
- messages:      会话消息，按 user_id 绑定，跨设备同步
- role_changes:  角色变更留痕（谁在什么时间把谁从什么角色改成了什么角色）
- security_logs: 安全事件（越权访问被拦截等），供管理员回查
- runtime_config: 运行时参数当前值（调试页修改，保存即热生效）
- config_changes: 运行参数修改历史（谁在什么时间把什么参数从多少改成多少）

历史说明：数据飞轮（review_queue / qa_library 表及 messages 的 feedback 列）已于
2026-09-22 整体移除；旧库中已存在的表与列保留不删，只是代码不再读写。
"""

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from app.config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT NOT NULL,
    title       TEXT NOT NULL,
    size        INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'processing',
    error       TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    heading_path TEXT NOT NULL DEFAULT '',
    content      TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON chunks(document_id);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    intent      TEXT,
    answer      TEXT,
    retrieval   TEXT,
    question    TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_user_id ON messages(user_id, id);

CREATE TABLE IF NOT EXISTS logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    question    TEXT NOT NULL,
    intent_name TEXT NOT NULL,
    trace       TEXT NOT NULL,
    total_ms    INTEGER NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_logs_user_id ON logs(user_id, id);

CREATE TABLE IF NOT EXISTS role_changes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    operator_id      INTEGER NOT NULL REFERENCES users(id),
    target_user_id   INTEGER NOT NULL REFERENCES users(id),
    target_email     TEXT NOT NULL,
    old_role         TEXT NOT NULL,
    new_role         TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_role_changes_target ON role_changes(target_user_id, id);

CREATE TABLE IF NOT EXISTS security_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    user_email TEXT NOT NULL,
    action     TEXT NOT NULL,
    path       TEXT NOT NULL,
    detail     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_security_logs_id ON security_logs(id);

CREATE TABLE IF NOT EXISTS runtime_config (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_by  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config_changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL,
    old_value   TEXT NOT NULL,
    new_value   TEXT NOT NULL,
    updated_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_config_changes_key ON config_changes(key, id);
"""

# 内置测试账号（权限管理阶段的验收依据，见 test-data/accounts.md）
_BUILTIN_ACCOUNTS = [
    {"email": "admin@app.com", "password": "123456", "role": "admin"},
    {"email": "user@app.com", "password": "123456", "role": "user"},
]

# 旧库迁移：已存在的表补充后来新增的列（幂等，每次启动检查一遍）。
# 第三列为加列后需立即执行的修补语句：旧库的内置 admin 在加 role 列时
# 会拿到 DEFAULT 'user'，需要在迁移当场修正为 admin（只此一次，不覆盖后续人工变更）。
_MIGRATIONS = [
    (
        "users",
        "role",
        "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'",
        ["UPDATE users SET role='admin' WHERE email='admin@app.com'"],
    ),
]


class Database:
    def __init__(self):
        path = Path(settings.sqlite_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.Lock()
        with self._lock:
            self.conn.executescript(_SCHEMA)
            self._migrate()
            self.conn.commit()
        self._seed_accounts()

    def _migrate(self) -> None:
        """给旧库已存在的表补列。CREATE TABLE IF NOT EXISTS 不会更新旧表结构。"""
        for table, column, ddl, after_ddl in _MIGRATIONS:
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.conn.execute(ddl)
                for stmt in after_ddl:
                    self.conn.execute(stmt)

    def _seed_accounts(self) -> None:
        """首次启动时写入内置测试账号（已存在则跳过）。"""
        from app.core.security import generate_salt, hash_password

        for account in _BUILTIN_ACCOUNTS:
            rows = self.query(
                "SELECT id FROM users WHERE email=?", (account["email"],)
            )
            if rows:
                continue
            salt = generate_salt()
            password_hash = hash_password(account["password"], salt)
            self.execute(
                "INSERT INTO users (email, password_hash, salt, role, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (account["email"], password_hash, salt, account["role"], now()),
            )

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()


_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database()
    return _db


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")
