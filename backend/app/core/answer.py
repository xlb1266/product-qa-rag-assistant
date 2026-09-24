"""基于检索知识与对话历史的回答生成。

提示词中三个 %s 依次填充【知识】【对话历史】【用户最新消息】。
DeepSeek 以 JSON 模式输出 {useful, content}：useful 表示知识是否足以作答。
"""

import json
import re
from dataclasses import dataclass

from app.core.llm import get_llm_client
from app.core.retrieval import RetrievedChunk
from app.core.runtime_config import get_runtime

ANSWER_PROMPT_TEMPLATE = """## 角色
你是产品咨询智能客服助手，你需要根据对话历史、用户最新消息、以及知识回答用户问题

## 回复原则
1. 简洁明确：直接回答，不绕弯，不要超过100字
2. 友好专业：保持礼貌，体现专业性
3. 客观公正：遵循客观事实，不要胡编乱造，所有回答的内容都需要在【知识】内容中有迹可循，宁可不答也不要乱答
4. 如果提供的知识与用户问题相关，正常作答，并将 useful=true；如果不相关或不足以解决，将 useful=false，并回复用户：暂时不能回答这个问题
5. 用中文回复，不超过100字

## 知识
{
%s
}

## 对话历史
{
%s
}

## 用户最新消息
{
%s
}

## 输出要求
必须使用以下JSON格式返回，不得包含任何其他文本：

```json
{
  "useful": boolean,
  "content": "string"
}
```
"""

FALLBACK_REPLY = "暂时不能回答这个问题"
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


@dataclass
class AnswerResult:
    useful: bool
    content: str


def _format_knowledge(chunks: list[RetrievedChunk]) -> str:
    """把检索到的片段编号列成知识文本；无结果时明确告知。"""
    if not chunks:
        return "（未检索到相关知识）"
    blocks: list[str] = []
    for i, c in enumerate(chunks, 1):
        source = c.heading_path or c.document_title
        blocks.append(f"【{i}】来源：{source}\n{c.content}")
    return "\n\n".join(blocks)


def _format_history(history: list[dict]) -> str:
    if not history:
        return "（无，当前为首轮对话）"
    lines = [
        f"{'用户' if m['role'] == 'user' else '客服'}：{m['content']}" for m in history
    ]
    return "\n".join(lines)


def build_answer_prompt(
    chunks: list[RetrievedChunk], history: list[dict], message: str
) -> str:
    """把知识、历史、最新消息填入提示词的三个占位符。"""
    prompt = ANSWER_PROMPT_TEMPLATE.replace("%s", _format_knowledge(chunks), 1)
    prompt = prompt.replace("%s", _format_history(history), 1)
    return prompt.replace("%s", message, 1)


def _extract_json(text: str) -> dict:
    m = _JSON_BLOCK_RE.search(text)
    candidate = (m.group(1) if m else text).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start != -1 and end > start:
            return json.loads(candidate[start : end + 1])
        raise


def assess_answerability(
    chunks: list[RetrievedChunk], rerank_used: bool
) -> tuple[bool, str]:
    """代码层拒答预判：在调用 LLM 之前判断证据是否足以作答。

    - 检索结果为空：直接拒答，不进 LLM；
    - 有真实 rerank 分数（未降级、未 mock）且最高分低于 answerability_threshold：拒答；
    - 重排降级或 mock 模式没有真实分数：跳过分数判断，只保留空结果拒答。

    返回 (是否可答, 拒答原因)；可答时原因为空字符串。
    chat.py 与评测脚本共用此实现，保证线上链路与评测口径一致。
    """
    if not chunks:
        return False, "检索结果为空"
    if rerank_used:
        threshold = float(get_runtime().get("retrieval.answerability_threshold") or 0.0)
        top_score = max(c.score for c in chunks)
        if top_score < threshold:
            return False, f"最高相关度 {top_score:.4f} 低于拒答阈值 {threshold}"
    return True, ""


async def generate_answer(
    history: list[dict], message: str, chunks: list[RetrievedChunk]
) -> AnswerResult:
    """调用 DeepSeek 基于知识生成回答。

    解析失败时降级为 useful=False，保证链路不中断。
    """
    content = await get_llm_client().chat(
        messages=[{"role": "user", "content": build_answer_prompt(chunks, history, message)}],
        temperature=0.3,
        json_mode=True,
    )

    try:
        data = _extract_json(content)
        useful = bool(data.get("useful", False))
        text = str(data.get("content") or "").strip() or FALLBACK_REPLY
    except (json.JSONDecodeError, ValueError, AttributeError):
        return AnswerResult(useful=False, content=FALLBACK_REPLY)

    if not useful:
        text = FALLBACK_REPLY
    return AnswerResult(useful=useful, content=text)
