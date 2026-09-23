"""BM25 关键词检索。

分词策略：中文按字符二元切分（bigram，中文检索经典做法，零依赖），
英文/数字按整词切分（小写化）。索引在进程内缓存，文档增删后失效重建。
"""

import math
import re
from collections import Counter

from app.db.database import get_db

_K1 = 1.5  # 词频饱和参数
_B = 0.75  # 文档长度归一化参数

_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_EN_WORD_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """中文段切 bigram（单字段保留单字），英文/数字按整词小写化。"""
    text = text.lower()
    tokens: list[str] = []
    for seg in _CJK_RE.findall(text):
        if len(seg) == 1:
            tokens.append(seg)
        else:
            tokens.extend(seg[i : i + 2] for i in range(len(seg) - 1))
    tokens.extend(_EN_WORD_RE.findall(text))
    return tokens


class BM25Index:
    def __init__(self, docs: list[dict]) -> None:
        """docs 为 [{"id": chunk_id, "content": str}]。"""
        self._ids = [d["id"] for d in docs]
        self._doc_tokens = [tokenize(d["content"]) for d in docs]
        self._doc_len = [len(t) for t in self._doc_tokens]
        self._avgdl = sum(self._doc_len) / max(len(docs), 1) or 1.0
        self._tf = [Counter(tokens) for tokens in self._doc_tokens]
        df: Counter = Counter()
        for tokens in self._doc_tokens:
            df.update(set(tokens))
        self._df = df
        self._n = len(docs)

    def search(self, query: str, top_n: int = 10) -> list[tuple[int, float]]:
        """返回 [(chunk_id, bm25 原始分数)]，按分数降序，只返回得分 > 0 的文档。"""
        q_tokens = tokenize(query)
        scores: list[tuple[int, float]] = []
        for i in range(self._n):
            s = 0.0
            dl_norm = 1 - _B + _B * self._doc_len[i] / self._avgdl
            for qt in q_tokens:
                tf = self._tf[i].get(qt, 0)
                if tf == 0:
                    continue
                idf = math.log(1 + (self._n - self._df[qt] + 0.5) / (self._df[qt] + 0.5))
                s += idf * tf * (_K1 + 1) / (tf + _K1 * dl_norm)
            if s > 0:
                scores.append((self._ids[i], s))
        scores.sort(key=lambda x: -x[1])
        return scores[:top_n]

    @property
    def size(self) -> int:
        return self._n


_index: BM25Index | None = None


def get_bm25_index() -> BM25Index:
    """惰性构建全量 chunks 的 BM25 索引（进程内缓存）。"""
    global _index
    if _index is None:
        rows = get_db().query("SELECT id, content FROM chunks")
        _index = BM25Index([dict(r) for r in rows])
    return _index


def invalidate_bm25_index() -> None:
    """文档/分块增删后调用，下次检索时自动重建。"""
    global _index
    _index = None
