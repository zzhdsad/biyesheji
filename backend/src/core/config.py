"""应用配置：所有敏感信息通过 .env 注入，禁止硬编码。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 应用
    APP_NAME: str = "Knowledge Platform API"
    VERSION: str = "0.1.0"
    ENV: str = "dev"  # dev / test / prod
    API_PREFIX: str = "/api/v1"
    CORS_ORIGINS: list[str] = ["http://localhost:3000"]
    LOG_LEVEL: str = "INFO"

    # 数据库 / 缓存 / 向量库
    DATABASE_URL: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/knowledge_platform"
    )
    REDIS_URL: str = "redis://localhost:6379/0"
    MILVUS_URI: str = "http://localhost:19530"
    MILVUS_COLLECTION: str = "document_chunks"

    # 安全
    SECRET_KEY: str = "change-me-in-production"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440

    # 模型服务
    LLM_BASE_URL: str = "http://localhost:8000/v1"
    LLM_API_KEY: str = "EMPTY"
    LLM_MODEL: str = "Qwen2.5-14B-Instruct-AWQ"
    EMBEDDING_MODEL: str = "BAAI/bge-m3"
    RERANK_MODEL: str = "BAAI/bge-reranker-v2-m3"

    # 检索参数
    RETRIEVAL_TOP_K: int = 10
    RERANK_TOP_N: int = 5
    HYDE_ENABLED: bool = False
    HISTORY_WINDOW: int = 3

    # 文档
    UPLOAD_DIR: str = "uploads"
    MAX_FILE_SIZE_MB: int = 50


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
