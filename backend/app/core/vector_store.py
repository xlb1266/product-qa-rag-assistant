"""FAISS 向量库管理。

- IndexIDMap2 包 IndexFlatIP：向量归一化后内积 = 余弦相似度
- IDMap2 支持按 ID 删除（remove_ids），删除文档时同步清掉对应向量
- 每次增删后立即持久化到磁盘，重启自动加载
"""

import threading
from pathlib import Path

import faiss
import numpy as np

from app.config import settings


class VectorStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.path = Path(settings.faiss_index_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.index = faiss.read_index(str(self.path))
        else:
            self.index = faiss.IndexIDMap2(
                faiss.IndexFlatIP(settings.embedding_dim)
            )

    def add(self, ids: list[int], vectors: list[list[float]]) -> None:
        """写入一批向量（内部做 L2 归一化）并持久化。"""
        if not ids:
            return
        with self._lock:
            arr = np.asarray(vectors, dtype="float32")
            faiss.normalize_L2(arr)
            self.index.add_with_ids(arr, np.asarray(ids, dtype="int64"))
            self._save()

    def remove(self, ids: list[int]) -> None:
        """按 FAISS ID 删除向量并持久化。"""
        if not ids:
            return
        with self._lock:
            if self.index.ntotal == 0:
                return
            self.index.remove_ids(np.asarray(ids, dtype="int64"))
            self._save()

    def search(self, vector: list[float], k: int = 5) -> list[tuple[int, float]]:
        """检索最相似的 k 个向量，返回 [(faiss_id, 相似度)]。"""
        with self._lock:
            if self.index.ntotal == 0:
                return []
            arr = np.asarray([vector], dtype="float32")
            faiss.normalize_L2(arr)
            scores, ids = self.index.search(arr, min(k, self.index.ntotal))
            return [
                (int(i), float(s))
                for i, s in zip(ids[0], scores[0])
                if i != -1
            ]

    def _save(self) -> None:
        faiss.write_index(self.index, str(self.path))

    @property
    def count(self) -> int:
        return self.index.ntotal


_store: VectorStore | None = None


def get_vector_store() -> VectorStore:
    global _store
    if _store is None:
        _store = VectorStore()
    return _store
