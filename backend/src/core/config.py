"""应用配置：所有敏感信息通过 .env 注入，禁止硬编码。"""

import os
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

    # 安全（JWT 鉴权）
    SECRET_KEY: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440  # 7 天

    # 模型服务
    LLM_BASE_URL: str = "http://localhost:8000/v1"
    LLM_API_KEY: str = "EMPTY"
    LLM_MODEL: str = "Qwen2.5-14B-Instruct-AWQ"
    EMBEDDING_MODEL: str = "BAAI/bge-m3"
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
    # HuggingFace 镜像：国内 huggingface.co 不可达，默认走 hf-mirror.com（可经 .env 覆盖）
    HF_ENDPOINT: str = "https://hf-mirror.com"
    HF_HUB_DOWNLOAD_TIMEOUT: int = 60

    # 检索参数
    RECALL_TOP_K: int = 50  # 每路召回数量（送入 RRF 融合）
    RERANK_TOP_N: int = 5  # 精排后返回给 LLM 的最终数量
    RRF_K: int = 60  # RRF 融合平滑常数
    # 相关性门槛（BUSINESS_RULES §6：检索相关度 < 0.3 时拒答）
    RELEVANCE_THRESHOLD: float = 0.3
    # mock（确定性伪重排，开发/测试）/ flagreranker（BGE-Reranker-v2-m3 真实精排）
    RERANK_BACKEND: str = "mock"
    RERANK_MODEL: str = "BAAI/bge-reranker-v2-m3"  # HuggingFace 模型 ID
    # 本地模型路径（离线/生产场景）：设了优先用本地路径，避免运行时下载
    # 为空则按 RERANK_MODEL 从 HuggingFace 拉取（首拉约 2.3GB，国内走 HF_ENDPOINT 镜像）
    RERANK_MODEL_PATH: str | None = None
    # 设备：cpu / cuda:0 / mps；CPU 上自动禁用 fp16
    RERANK_DEVICE: str = "cpu"
    # HyDE 查询改写（TECH_DESIGN §4.5：检索前用小模型生成假设答案替换原问题）
    # 默认开启（用户需求）；开发期无小模型服务时设 HYDE_BACKEND=mock 或 HYDE_ENABLED=false
    HYDE_ENABLED: bool = True
    HYDE_BACKEND: str = "mock"  # mock / openai
    HYDE_MODEL: str = "Qwen/Qwen2.5-1.5B-Instruct"
    # HyDE 小模型 API 地址：为空则复用 LLM_BASE_URL
    # Ollama：http://localhost:11434/v1（Ollama ≥0.1.x 兼容 OpenAI /v1）
    # vLLM  ：http://localhost:8001/v1（vllm serve Qwen/Qwen2.5-1.5B-Instruct --port 8001）
    HYDE_BASE_URL: str | None = None
    HISTORY_WINDOW: int = 5  # BUSINESS_RULES §6：默认携带最近 5 轮历史
    # 会话缓存（TECH_DESIGN：conversation:{conv_id}:history TTL 24h）
    HISTORY_TTL_SECONDS: int = 86400

    # 回收站
    TRASH_RETENTION_DAYS: int = 7  # BUSINESS_RULES §5：默认 7 天，可配置 1-30 天

    # 文档
    UPLOAD_DIR: str = "uploads"
    MAX_FILE_SIZE_MB: int = 50  # BUSINESS_RULES §4：默认 50MB（可配置）

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

    # 开发环境默认管理员（系统首次启动时若 users 表为空则自动创建）
    DEFAULT_ADMIN_EMAIL: str = "admin@company.com"
    DEFAULT_ADMIN_USERNAME: str = "admin"
    DEFAULT_ADMIN_PASSWORD: str = "admin123456"

    # 评估质量门禁（AGENTS.md：answer_correctness ≥ 0.75 才允许合入）
    EVAL_ACCURACY_THRESHOLD: float = 0.75


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

# 注入 HuggingFace 下载相关环境变量（huggingface_hub 在首次下载时读取）。
# 必须在 FlagEmbedding/transformers 触发下载前设置；config 在启动早期被各模块导入。
os.environ.setdefault("HF_ENDPOINT", settings.HF_ENDPOINT)
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", str(settings.HF_HUB_DOWNLOAD_TIMEOUT))
