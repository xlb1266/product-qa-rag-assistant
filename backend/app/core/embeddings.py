"""阿里百炼 text-embedding-v4 向量化客户端（OpenAI 兼容模式）。

批量规则：每次请求最多 embedding_batch_size（默认 10）段，超出自动分批。
"""

import httpx

from app.config import settings


class EmbeddingError(Exception):
    """向量化失败（未配置 Key、Key 无效、超限、网络错误等）。"""


class EmbeddingClient:
    def __init__(self) -> None:
        if not settings.dashscope_api_key:
            raise EmbeddingError(
                "未配置 DASHSCOPE_API_KEY，请在 backend/.env 中填入阿里百炼 API Key"
            )
        self._client = httpx.AsyncClient(
            base_url=settings.dashscope_base_url,
            headers={"Authorization": f"Bearer {settings.dashscope_api_key}"},
            timeout=60.0,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """把一批文本向量化，返回与输入顺序对应的向量列表。"""
        result: list[list[float]] = []
        batch_size = settings.embedding_batch_size
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            try:
                resp = await self._client.post(
                    "/embeddings",
                    json={
                        "model": settings.embedding_model,
                        "input": batch,
                        "dimensions": settings.embedding_dim,
                        "encoding_format": "float",
                    },
                )
            except httpx.HTTPError as e:
                raise EmbeddingError(f"请求百炼服务失败: {e}") from e

            if resp.status_code != 200:
                raise EmbeddingError(
                    f"百炼返回 HTTP {resp.status_code}: {resp.text[:300]}"
                )

            data = resp.json().get("data")
            if not data or len(data) != len(batch):
                raise EmbeddingError(
                    f"百炼返回向量数量({len(data or [])})与输入段数({len(batch)})不一致"
                )
            # 按 index 排序，确保与输入顺序一一对应
            data.sort(key=lambda d: d["index"])
            result.extend(d["embedding"] for d in data)
        return result


_client: EmbeddingClient | None = None


def get_embedding_client() -> EmbeddingClient:
    global _client
    if _client is None:
        _client = EmbeddingClient()
    return _client
