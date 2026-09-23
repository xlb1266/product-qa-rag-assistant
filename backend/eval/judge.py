"""LLM-as-a-Judge 生成段裁判（v2 新增，独立于业务代码）。

对 answerable 题目"检索成功作答"（useful=true）的回答打两个 0~1 分：
- faithfulness：答案中每个事实性陈述是否被当次检索到的上下文支持（防幻觉核心指标）；
- answer_relevance：答案是否切题、是否实际回答了用户的问题。

约束：
- 裁判上下文必须取自当次评测的 RetrievalResult（由调用方传入 chunks 内容），
  不允许为裁判重新检索——两次检索结果可能不同，评的就不是同一次回答；
- 复用 app/core/llm.py 的 DeepSeek 客户端，json_mode、temperature=0；
- prompt 内置 0 / 0.5 / 1 分锚点，降低打分离散度；
- 逐条输出 reason，落盘明细供人工抽查裁判本身靠不靠谱。

已知局限：裁判与被评模型同源（都是 DeepSeek），存在自评偏袒风险，
需结合 reason 人工抽查校准，该局限已在基线报告 v2 增补节如实记录。
"""

import json
import re
from dataclasses import dataclass

from app.core.llm import LLMError, get_llm_client

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)

JUDGE_SYSTEM_PROMPT = (
    "你是严格的 RAG 问答质量裁判，根据给定的检索上下文评估生成答案的质量。"
    "你必须只输出一个 JSON 对象，不得包含任何其他文本。"
)

JUDGE_USER_TEMPLATE = """## 待评估数据

【用户问题】
{query}

【检索上下文】（回答生成时模型实际看到的全部知识）
{contexts}

【生成的答案】
{answer}

## 评分 1：faithfulness（忠实度）
答案中的每个事实性陈述是否都能在【检索上下文】中找到依据。
锚点：
- 1.0：答案中所有事实性陈述都能被上下文直接支持，没有引入上下文之外的信息；
- 0.5：核心结论有上下文支持，但夹杂少量上下文未提及的细节或轻微夸大；
- 0.0：答案核心结论与上下文矛盾，或完全是上下文中不存在的编造。

## 评分 2：answer_relevance（答案相关性）
答案是否切题、是否实际回答了用户的问题。
锚点：
- 1.0：直接回答了问题的核心；
- 0.5：只回答了问题的一部分，或答到了相邻话题；
- 0.0：答非所问，或完全没有回应问题。

## 输出格式
只输出如下 JSON（分数为 0 到 1 之间的小数）：
{{"faithfulness": 0.0, "answer_relevance": 0.0, "reason": "两个分数的判定理由；若忠实度不满 1 分，逐条指出哪些陈述缺乏上下文支持"}}"""


@dataclass
class JudgeVerdict:
    faithfulness: float
    answer_relevance: float
    reason: str


def _parse_verdict(raw: str) -> JudgeVerdict:
    """解析裁判 JSON 输出；DeepSeek 偶发包一层 markdown 代码块，先直接解析再兜底剥离。"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = _JSON_BLOCK_RE.search(raw)
        if not match:
            raise ValueError(f"裁判输出不是合法 JSON：{raw[:200]}")
        data = json.loads(match.group(1))

    faithfulness = float(data["faithfulness"])
    relevance = float(data["answer_relevance"])
    reason = str(data.get("reason", "")).strip()
    # 分数夹取到 [0, 1]，避免裁判输出越界值污染均值
    faithfulness = round(min(max(faithfulness, 0.0), 1.0), 2)
    relevance = round(min(max(relevance, 0.0), 1.0), 2)
    return JudgeVerdict(faithfulness=faithfulness, answer_relevance=relevance, reason=reason)


async def judge_answer(query: str, contexts: list[str], answer: str) -> JudgeVerdict:
    """调用裁判 LLM 打分。contexts 必须是当次评测检索结果的内容列表。"""
    if not contexts:
        contexts = ["（空：当次检索未返回任何内容）"]
    context_text = "\n\n".join(f"【{i}】{c}" for i, c in enumerate(contexts, 1))
    user_prompt = JUDGE_USER_TEMPLATE.format(
        query=query, contexts=context_text, answer=answer
    )
    client = get_llm_client()
    messages = [
        {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    raw = await client.chat(messages, temperature=0, json_mode=True)
    return _parse_verdict(raw)


class JudgeError(Exception):
    """裁判最终失败，不计入均值，由调用方单独计数。"""


async def judge_answer_with_retry(
    query: str, contexts: list[str], answer: str, retries: int = 1
) -> JudgeVerdict:
    """裁判调用失败（网络/解析）重试一次；仍失败抛出 JudgeError，由调用方记录为 judge_error。"""
    last_error: Exception | None = None
    for _ in range(retries + 1):
        try:
            return await judge_answer(query, contexts, answer)
        except (LLMError, ValueError, KeyError, json.JSONDecodeError) as e:
            last_error = e
    raise JudgeError(f"裁判调用失败（已重试 {retries} 次）：{last_error}") from last_error
