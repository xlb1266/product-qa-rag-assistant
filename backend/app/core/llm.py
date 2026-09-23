"""DeepSeek 大模型客户端：意图识别与闲聊回复共用（OpenAI 兼容模式）。"""

import httpx

from app.config import settings


class LLMError(Exception):
    """大模型调用失败（未配置 Key、Key 无效、网络错误等）。"""


class DeepSeekClient:
    def __init__(self) -> None:
        if not settings.deepseek_api_key:
            raise LLMError(
                "未配置 DEEPSEEK_API_KEY，请在 backend/.env 中填入 DeepSeek API Key"
            )
        self._client = httpx.AsyncClient(
            base_url=settings.deepseek_base_url,
            headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
            timeout=60.0,
        )

    async def chat(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        json_mode: bool = False,
    ) -> str:
        """非流式对话补全，返回首条回复文本。

        json_mode=True 时启用 DeepSeek 的 JSON Output
        （要求提示词中已明确要求输出 JSON），适合意图识别这类结构化任务。
        """
        body: dict = {
            "model": settings.deepseek_model,
            "messages": messages,
            "temperature": temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        try:
            resp = await self._client.post("/chat/completions", json=body)
        except httpx.HTTPError as e:
            raise LLMError(f"请求 DeepSeek 服务失败: {e}") from e

        if resp.status_code != 200:
            raise LLMError(f"DeepSeek 返回 HTTP {resp.status_code}: {resp.text[:300]}")

        choices = resp.json().get("choices")
        if not choices or not choices[0].get("message", {}).get("content"):
            raise LLMError("DeepSeek 返回内容为空或格式异常")
        return choices[0]["message"]["content"]


_client: DeepSeekClient | None = None


def get_llm_client() -> DeepSeekClient:
    global _client
    if _client is None:
        _client = DeepSeekClient()
    return _client
