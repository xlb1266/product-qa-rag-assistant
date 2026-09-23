"""意图识别模块：指代消解 + 三分类（SUPPORT 产品咨询 / FEEDBACK 问题反馈 / CHAT 闲聊）。

提示词中两个 %s 依次填充【对话历史】【用户最新消息】。
"""

import json
import re
from dataclasses import dataclass

from app.core.llm import get_llm_client

SUPPORT = "SUPPORT"
FEEDBACK = "FEEDBACK"
CHAT = "CHAT"
VALID_INTENTS = (SUPPORT, FEEDBACK, CHAT)

INTENT_PROMPT_TEMPLATE = """## 角色
你是产品咨询智能客服系统的专业意图识别器。
你的输出将被直接用于自动路由和客服决策，请严格遵循规则，不得自由发挥。

## 总体目标
基于【对话历史】和【用户最新消息】，完成以下 两个任务：

1. 指代消解（必须先完成）
2. 意图识别（基于指代消解后的问题）

## 任务一：指代消解

目标：
结合【对话历史】，对【用户最新消息】中的所有指代性表达进行消解，你需要生成一条单句、语义完整、无任何模糊指代的用户问题

处理规则：
1.必须结合【对话历史】进行还原
2.如果用户问题本身已经明确，不要强行改写
3.不得引入对话中不存在的新信息
4.输出必须是一个完整问题句

输出字段：
resolved_question

## 任务二：意图识别
基于 resolved_question，结合【对话历史】信息进行理解，将其准确分类到以下三个意图之一，并且按照顺序依次判断：

1. **产品咨询(SUPPORT)** , 判断标准为 {
  产品功能介绍/产品价值/能解决什么问题
  使用方法/操作步骤/使用教程
  下载安装/注册登录/账号相关
  产品定价/会员/付费
  与竞品的比较
  翻译/语言支持/语言学习相关
  播放/播客/朗读
  聊天/加好友/社交/翻译对话相关
}
2. **问题反馈(FEEDBACK)**, 判断标准为 {
  用户明确提出对产品功能优化建议、希望增加功能、优化建议、故障反馈，比如：功能异常、报错、打不开、点不动、卡顿、无法使用等
}
3. **闲聊(CHAT)**，判断标准为 {
  当不属于以上两类时，一律归为 CHAT，例如问候、寒暄、玩笑、调侃、情感倾诉、模糊无意义输入
}


## 输入信息

### 对话历史
{
%s
}

### 用户最新消息
{
%s
}

## 输出要求
1. **必须**使用以下JSON格式返回，不得包含任何其他文本：

```json
{
  "confidence": 0.0-1.0",
  "intent": "SUPPORT" | "FEEDBACK" | "CHAT",
  "resolved_question": "指代消解后的明确问题",
  "reason": "简要分类理由（说明上下文如何影响判断）"
}
```
"""

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


@dataclass
class IntentResult:
    intent: str
    confidence: float
    resolved_question: str
    reason: str


def build_prompt(history: list[dict], question: str) -> str:
    """把对话历史与最新消息填入提示词模板的两个 %s 占位符。"""
    prompt = INTENT_PROMPT_TEMPLATE.replace("%s", _format_history(history), 1)
    return prompt.replace("%s", question, 1)


def _format_history(history: list[dict]) -> str:
    if not history:
        return "（无，当前为首轮对话）"
    lines = [
        f"{'用户' if m['role'] == 'user' else '客服'}：{m['content']}" for m in history
    ]
    return "\n".join(lines)


def _extract_json(text: str) -> dict:
    """从模型输出中提取 JSON 对象，容忍 markdown 代码块和首尾杂文本。"""
    m = _JSON_BLOCK_RE.search(text)
    candidate = (m.group(1) if m else text).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start != -1 and end > start:
            return json.loads(candidate[start : end + 1])
        raise


async def detect_intent(history: list[dict], question: str) -> IntentResult:
    """识别用户消息的意图。

    history 为 [{"role": "user"|"assistant", "content": "..."}]。
    解析失败或 intent 非法时降级为 CHAT（闲聊是兜底意图），保证问答链路不中断。
    """
    content = await get_llm_client().chat(
        messages=[{"role": "user", "content": build_prompt(history, question)}],
        temperature=0.1,
        json_mode=True,
    )

    try:
        data = _extract_json(content)
    except (json.JSONDecodeError, ValueError):
        return IntentResult(
            intent=CHAT,
            confidence=0.0,
            resolved_question=question,
            reason=f"意图识别输出解析失败，已降级为闲聊。原始输出：{content[:200]}",
        )

    intent = str(data.get("intent", "")).upper()
    if intent not in VALID_INTENTS:
        intent = CHAT
    try:
        confidence = min(max(float(data.get("confidence", 0.0)), 0.0), 1.0)
    except (TypeError, ValueError):
        confidence = 0.0
    resolved = str(data.get("resolved_question") or "").strip() or question

    return IntentResult(
        intent=intent,
        confidence=confidence,
        resolved_question=resolved,
        reason=str(data.get("reason", "")).strip(),
    )
