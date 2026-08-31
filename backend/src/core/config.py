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
    # openai（OpenAI 兼容接口，如 vLLM）/ mock（确定性假回答，开发/测试）
    LLM_BACKEND: str = "openai"
    LLM_TIMEOUT_SECONDS: int = 120

    # 向量化（TECH_DESIGN：BGE-M3 稠密 1024d + 稀疏向量）
    # flagembedding（本地 FlagEmbedding，需下载模型）/ mock（确定性伪向量，开发/测试）
    EMBEDDING_BACKEND: str = "flagembedding"
    EMBEDDING_DEVICE: str = "cpu"
    EMBEDDING_BATCH_SIZE: int = 16
    MILVUS_DIM: int = 1024
    VECTORIZE_BATCH_SIZE: int = 32

    # 检索参数
    RECALL_TOP_K: int = 50  # 每路召回数量（送入 RRF 融合）
    RERANK_TOP_N: int = 5  # 精排后返回给 LLM 的最终数量
    RRF_K: int = 60  # RRF 融合平滑常数
    RERANK_BACKEND: str = "mock"  # mock / flagreranker
    RERANK_MODEL: str = "BAAI/bge-reranker-v2-m3"
    # HyDE 查询改写（TECH_DESIGN：检索前用小模型生成假设答案替换原问题）
    HYDE_ENABLED: bool = False
    HYDE_BACKEND: str = "mock"  # mock / openai
    HYDE_MODEL: str = "Qwen/Qwen2.5-1.5B-Instruct"
    HYDE_BASE_URL: str | None = None  # 默认复用 LLM_BASE_URL
    HISTORY_WINDOW: int = 3
    # 会话缓存（TECH_DESIGN：conversation:{conv_id}:history TTL 24h）
    HISTORY_TTL_SECONDS: int = 86400

    # 文档
    UPLOAD_DIR: str = "uploads"
    MAX_FILE_SIZE_MB: int = 50

    # 解析 / 切片（TECH_DESIGN：结构感知切片，512~1024 tokens，overlap 50~100）
    PARSE_BACKEND: str = "background"  # background（进程内线程池）/ celery（Redis 队列）
    CHUNK_SIZE_TOKENS: int = 768
    CHUNK_OVERLAP_TOKENS: int = 100
    CHUNK_MIN_TOKENS: int = 32

    # 文件存储：local（本地磁盘）/ minio（S3 兼容对象存储）
    STORAGE_BACKEND: str = "local"
    MINIO_ENDPOINT: str = "localhost:9000"
    MINIO_ACCESS_KEY: str = "minioadmin"
    MINIO_SECRET_KEY: str = "minioadmin"
    MINIO_BUCKET: str = "documents"

    # 开发环境默认管理员（接入 JWT 鉴权前的占位）
    DEFAULT_ADMIN_EMAIL: str = "admin@example.com"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
