"""运行时参数管理：调试页修改参数 -> 校验 -> 热生效（不重启）+ 全量留痕。

设计：
- 参数定义集中在 _PARAM_DEFS（类型/默认值/范围/说明），不在定义内的 key 一律拒绝
- 当前值存在内存单例（业务代码 get() 直接读，改完立即生效）+ runtime_config 表（重启后恢复）
- set_many 先整体校验，全部合法才应用——不会出现"改了一半"
- 每次变更写 config_changes（谁、何时、从多少改成多少），支持一键回滚到上一历史值
- 安全参数（密码加密规则、auth_secret、API Key）不进入本体系：Key 只在调试页打码只读展示
"""

import json
import threading
from dataclasses import dataclass
from typing import Any

from app.db.database import get_db, now


@dataclass(frozen=True)
class ParamDef:
    key: str
    label: str
    type: str  # "int" | "float" | "bool"
    default: Any
    min: float | None = None
    max: float | None = None
    group: str = ""


_PARAM_DEFS: dict[str, ParamDef] = {
    p.key: p
    for p in [
        ParamDef("retrieval.semantic_top_n", "每路召回条数（语义 / BM25）", "int", 10, 1, 20, "检索链路"),
        ParamDef("retrieval.candidate_top_n", "融合后送重排的候选条数", "int", 8, 1, 20, "检索链路"),
        ParamDef("retrieval.rerank_top_k", "重排后最终保留条数", "int", 4, 1, 10, "检索链路"),
        ParamDef("retrieval.rerank_threshold", "重排相关性阈值（低于此分过滤）", "float", 0.6, 0.0, 1.0, "检索链路"),
        ParamDef("chat.max_history", "传给模型的对话历史条数", "int", 20, 1, 50, "会话链路"),
        ParamDef(
            "debug.mock_third_party",
            "Mock 第三方调用（意图/回答/向量化/重排走模拟返回，不产生 API 费用）",
            "bool", False, None, None, "调试",
        ),
    ]
}


class ValidationError(ValueError):
    """参数值不合法（携带用户可读的中文原因）。"""


class RuntimeConfig:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, Any] = {p.key: p.default for p in _PARAM_DEFS.values()}
        self._load_from_db()

    def _load_from_db(self) -> None:
        """启动时用 runtime_config 表里保存过的值覆盖默认值。"""
        try:
            rows = get_db().query("SELECT key, value FROM runtime_config")
        except Exception:
            return  # 表未建好等极端情况：退回默认值
        for r in rows:
            if r["key"] in self._values:
                try:
                    self._values[r["key"]] = json.loads(r["value"])
                except json.JSONDecodeError:
                    continue

    def defs(self) -> list[ParamDef]:
        return list(_PARAM_DEFS.values())

    def get(self, key: str) -> Any:
        return self._values.get(key)

    def snapshot(self) -> dict[str, Any]:
        """当前全部参数值（调试页展示用）。"""
        return dict(self._values)

    def _validate(self, key: str, value: Any) -> Any:
        """校验单个值，返回规整后的值；不合法抛 ValidationError。"""
        d = _PARAM_DEFS.get(key)
        if d is None:
            raise ValidationError(f"未知参数：{key}")
        if d.type == "bool":
            if not isinstance(value, bool):
                raise ValidationError(f"{d.label}（{key}）必须是布尔值")
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            # bool 是 int 的子类，先排除；其余类型（字符串等）不允许
            if d.type == "int":
                raise ValidationError(f"{d.label}（{key}）必须是整数")
            raise ValidationError(f"{d.label}（{key}）必须是数字")
        if d.type == "int":
            if isinstance(value, float) and not value.is_integer():
                raise ValidationError(f"{d.label}（{key}）必须是整数")
            value = int(value)
        else:
            value = float(value)
        if d.min is not None and value < d.min:
            raise ValidationError(f"{d.label}（{key}）不能小于 {d.min}")
        if d.max is not None and value > d.max:
            raise ValidationError(f"{d.label}（{key}）不能大于 {d.max}")
        return value

    def set_many(self, updates: dict[str, Any], operator: str) -> dict[str, Any]:
        """批量更新：先全部校验，全部合法才应用（内存 + DB + 历史），否则整条拒绝。"""
        normalized: dict[str, Any] = {}
        for key, value in updates.items():
            normalized[key] = self._validate(key, value)

        applied: dict[str, dict[str, Any]] = {}
        with self._lock:
            for key, value in normalized.items():
                if self._values[key] == value:
                    continue  # 值未变化，不写库不留痕
                old = self._values[key]
                self._values[key] = value
                applied[key] = {"old": old, "new": value}
            if not applied:
                return {"ok": True, "changed": 0, "message": "参数与当前值相同，未变更"}

            db = get_db()
            for key, ch in applied.items():
                db.execute(
                    "INSERT INTO runtime_config (key, value, updated_by, updated_at)"
                    " VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                    " updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                    (key, json.dumps(ch["new"], ensure_ascii=False), operator, now()),
                )
                db.execute(
                    "INSERT INTO config_changes (key, old_value, new_value, updated_by, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        key,
                        json.dumps(ch["old"], ensure_ascii=False),
                        json.dumps(ch["new"], ensure_ascii=False),
                        operator,
                        now(),
                    ),
                )
        return {"ok": True, "changed": len(applied), "message": f"已更新 {len(applied)} 个参数并即时生效"}

    def rollback(self, key: str, operator: str) -> dict[str, Any]:
        """把 key 回滚到最近一次修改前的值（回滚本身也记入历史）。"""
        d = _PARAM_DEFS.get(key)
        if d is None:
            raise ValidationError(f"未知参数：{key}")
        rows = get_db().query(
            "SELECT old_value FROM config_changes WHERE key=? ORDER BY id DESC LIMIT 1",
            (key,),
        )
        if not rows:
            raise ValidationError(f"参数 {key} 没有修改历史，无法回滚")
        old_value = json.loads(rows[0]["old_value"])
        return self.set_many({key: old_value}, operator)


_runtime: RuntimeConfig | None = None


def get_runtime() -> RuntimeConfig:
    global _runtime
    if _runtime is None:
        _runtime = RuntimeConfig()
    return _runtime
