"""阿里百炼 qwen3-rerank 重排客户端（compatible-api，扁平请求结构）。"""

import httpx

from app.config import settings


class RerankError(Exception):
    """重排失败（未配置 Key、Key 无效、网络错误等）。"""


class Reranker:
    def __init__(self) -> None:
        if not settings.dashscope_api_key:
            raise RerankError(
                "未配置 DASHSCOPE_API_KEY，请在 backend/.env 中填入阿里百炼 API Key"
            )
        self._client = httpx.AsyncClient(
            timeout=60.0,
            headers={"Authorization": f"Bearer {settings.dashscope_api_key}"},
        )

    async def rerank(
        self, query: str, documents: list[str], top_n: int | None = None
    ) -> list[tuple[int, float]]:
        """对候选文档按与 query 的相关性重排。

        返回 [(documents 中的原始下标, relevance_score 0~1)]，按分数降序；
        top_n 为 None 时返回全部。
        """
        if not documents:
            return []
        body: dict = {
            "model": settings.rerank_model,
            "query": query,
            "documents": documents,
        }
        if top_n is not None:
            body["top_n"] = top_n

        try:
            resp = await self._client.post(settings.rerank_api_url, json=body)
        except httpx.HTTPError as e:
            raise RerankError(f"请求百炼重排服务失败: {e}") from e

        if resp.status_code != 200:
            raise RerankError(
                f"百炼重排返回 HTTP {resp.status_code}: {resp.text[:300]}"
            )

        results = resp.json().get("results")
        if results is None:
            raise RerankError(f"百炼重排返回缺少 results: {resp.text[:300]}")
        return [(int(r["index"]), float(r["relevance_score"])) for r in results]


_client: Reranker | None = None


def get_reranker() -> Reranker:
    global _client
    if _client is None:
        _client = Reranker()
    return _client
