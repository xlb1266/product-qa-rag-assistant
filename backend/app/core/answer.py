"""基于检索知识与对话历史的回答生成。

提示词中三个 %s 依次填充【知识】【对话历史】【用户最新消息】。
DeepSeek 以 JSON 模式输出 {useful, content, citations}：useful 表示知识是否足以作答，
citations 列出支撑回答的知识条目编号（对应【知识】中的【N】），由后端清洗后随回答落库。
"""

import json
import re
from dataclasses import dataclass, field

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
6. 引用溯源：content 中的每个事实都必须来自【知识】的某几条编号条目；在 citations 数组中列出支撑本次回答的条目编号（如 [1, 3]），正文里不要写编号；没有可引用的条目时 citations 为空数组 []；只能使用【知识】中真实存在的编号，不要编造

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
  "content": "string",
  "citations": [1, 3]
}
```
"""

FALLBACK_REPLY = "暂时不能回答这个问题"
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


@dataclass
class AnswerResult:
    useful: bool
    content: str
    citations: list[int] = field(default_factory=list)  # 支撑回答的知识条目编号（1 起）


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


def _clean_citations(raw: object, n_chunks: int) -> list[int]:
    """清洗模型返回的引用编号，脏编号不透传。

    只接受 1~n_chunks 范围内的整数（bool 是 int 子类，先排除），
    越界、字符串、浮点、重复一律丢弃；非列表整体视为无效，按空数组兜底。
    """
    if not isinstance(raw, list):
        return []
    cleaned: list[int] = []
    for v in raw:
        if isinstance(v, bool) or not isinstance(v, int):
            continue
        if 1 <= v <= n_chunks and v not in cleaned:
            cleaned.append(v)
    return cleaned


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

    解析失败时降级为 useful=False，保证链路不中断；
    citations 缺失或无效单独按空数组兜底，不拖垮整体解析。
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
        citations = _clean_citations(data.get("citations"), len(chunks))
    except (json.JSONDecodeError, ValueError, AttributeError):
        return AnswerResult(useful=False, content=FALLBACK_REPLY)

    if not useful:
        text = FALLBACK_REPLY
        citations = []
    return AnswerResult(useful=useful, content=text, citations=citations)
