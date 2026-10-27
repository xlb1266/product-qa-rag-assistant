"""Markdown 按标题层级切分。

规则：
- 每遇到一个 # ~ ###### 标题就开一个新块，块内容 = 标题行 + 该节正文
- heading_path 记录完整的标题层级路径，如 "总标题 > 二级标题 > 三级标题"
- 超过 max_chars 的块按空行分段做二次切分，单段仍超长时按字符数硬切
"""

import re

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")

MAX_CHUNK_CHARS = 800


def split_markdown(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[dict]:
    """把 Markdown 文本切分为块列表，每块形如
    {"seq": 序号, "heading_path": 标题路径, "content": 块文本}"""
    chunks: list[dict] = []
    stack: list[tuple[int, str]] = []  # (标题级别, 标题文本)
    current: dict | None = None

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        content = "\n".join(current["lines"]).strip()
        if content:
            chunks.extend(_split_long(content, current["heading_path"], max_chars))
        current = None

    for line in text.splitlines():
        m = HEADING_RE.match(line)
        if m:
            flush()
            level, title = len(m.group(1)), m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            current = {
                "heading_path": " > ".join(t for _, t in stack),
                "lines": [line],
            }
        else:
            if current is None:
                # 首个标题之前存在正文的情况
                current = {"heading_path": "", "lines": []}
            current["lines"].append(line)
    flush()

    for i, chunk in enumerate(chunks):
        chunk["seq"] = i + 1
    return chunks


def _split_long(content: str, heading_path: str, max_chars: int = MAX_CHUNK_CHARS) -> list[dict]:
    if len(content) <= max_chars:
        return [{"heading_path": heading_path, "content": content}]

    pieces: list[str] = []
    buf = ""
    for para in (p.strip() for p in re.split(r"\n\s*\n", content)):
        if not para:
            continue
        if len(para) > max_chars:
            if buf:
                pieces.append(buf)
                buf = ""
            for i in range(0, len(para), max_chars):
                pieces.append(para[i : i + max_chars])
        elif len(buf) + len(para) + 2 <= max_chars:
            buf = f"{buf}\n\n{para}" if buf else para
        else:
            pieces.append(buf)
            buf = para
    if buf:
        pieces.append(buf)

    return [{"heading_path": heading_path, "content": piece} for piece in pieces]
