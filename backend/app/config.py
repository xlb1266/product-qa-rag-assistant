"""应用配置：从 .env 读取，所有第三方密钥集中在此。"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # DeepSeek 大模型
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    # 阿里百炼：向量模型（OpenAI 兼容模式）
    dashscope_api_key: str = ""
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_model: str = "text-embedding-v4"
    embedding_dim: int = 1024
    embedding_batch_size: int = 10

    # 阿里百炼：重排模型
    rerank_model: str = "qwen3-rerank"
    rerank_api_url: str = "https://dashscope.aliyuncs.com/compatible-api/v1/reranks"

    # FAISS 索引与 SQLite 数据库路径
    faiss_index_path: str = "data/index.faiss"
    sqlite_path: str = "data/knowledge.db"

    # 鉴权：Token 签名密钥与有效期（自签 HMAC-SHA256，零依赖）
    auth_secret: str = "dev-secret-change-me"
    token_expire_days: int = 7

    # 允许跨域的前端来源
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


settings = Settings()
