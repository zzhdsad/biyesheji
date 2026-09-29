"""应用配置：所有敏感信息通过 .env 注入，禁止硬编码。"""

import os
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 仅用于本地开发/测试的默认密钥；生产环境必须经环境变量 SECRET_KEY 覆盖，
# 否则启动即失败（BUG-026：固定密钥可被用于伪造 JWT）。
DEV_DEFAULT_SECRET_KEY = "hfimJesB4aztr41rt3zgnBWKgyY7VIgq5C0LmNoUCSHnUCicRImiHt-_wDpmmbYN"


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

    # 安全（JWT 鉴权）；默认值仅用于开发/测试，生产必须经 .env 覆盖为随机 ≥32 字节密钥
    SECRET_KEY: str = DEV_DEFAULT_SECRET_KEY
    JWT_ALGORITHM: str = "HS256"
    # 1440 分钟 = 24 小时（BUG-061：原注释误写为 7 天；前端 cookie max-age 已与之对齐）
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440

    # 模型服务
    LLM_BASE_URL: str = "http://localhost:8000/v1"
    LLM_API_KEY: str = "EMPTY"
    LLM_MODEL: str = "Qwen2.5-14B-Instruct-AWQ"
    EMBEDDING_MODEL: str = "BAAI/bge-m3"
    # openai / deepseek / qwen / ollama / custom（均为 OpenAI 兼容协议）
    LLM_BACKEND: str = "openai"
    LLM_TIMEOUT_SECONDS: int = 120
    # SSE 流式问答保活（BUG-035）：反向代理（如 nginx proxy_read_timeout=60s）
    # 会在检索阶段掐断"长时间无数据"的连接，故在业务事件之间插入注释帧心跳；
    # 总超时用于收敛卡住不返回的流（超时先发 error 事件再关闭）。
    SSE_HEARTBEAT_INTERVAL_SECONDS: int = 15
    SSE_TOTAL_TIMEOUT_SECONDS: int = 900

    # 向量化（TECH_DESIGN：BGE-M3 稠密 1024d + 稀疏向量）
    EMBEDDING_BACKEND: str = "flagembedding"
    EMBEDDING_DEVICE: str = "cpu"
    EMBEDDING_BATCH_SIZE: int = 16
    MILVUS_DIM: int = 1024
    VECTORIZE_BATCH_SIZE: int = 32

    # ── 批量向量化任务（文档重新向量化 / 资源批量挂载）的批次与并发 ──────────
    # 目标机器：15GB RAM + CPU BGE-M3 + Milvus Docker。默认一律取保守值：
    # 单进程、单 embedding worker、串行流水线，优先稳定而非吃满资源。
    VECTORIZE_JOB_DOC_BATCH: int = 8  # 文档批量向量化：每批文档数
    VECTORIZE_JOB_RESOURCE_BATCH: int = 16  # 资源批量挂载：每批资源数
    VECTORIZE_JOB_CHUNK_BATCH: int = 32  # 单次 embedding 的文本条数上限（限定内存峰值）
    VECTORIZE_JOB_FLUSH_EVERY: int = 1  # 每 N 个写入批次 flush 一次 Milvus（1=每批落盘）
    VECTORIZE_JOB_MAX_ITEMS: int = 5000  # 单个批量任务的条目上限（安全阀）
    # 并发策略（固定单 worker，不提供"调大即起飞"的开关）：
    # 一个 embedding worker（复用全局单例 BGE-M3，不启第二个模型实例）+
    # 一个 Milvus 写入端，批次内串行 embed→write→下一批；
    # 内存上界 = VECTORIZE_JOB_CHUNK_BATCH × 1024 维向量，不会随任务规模增长。
    # HuggingFace 镜像：国内 huggingface.co 不可达，默认走 hf-mirror.com（可经 .env 覆盖）
    HF_ENDPOINT: str = "https://hf-mirror.com"
    HF_HUB_DOWNLOAD_TIMEOUT: int = 60
    # 模型权重由镜像内置到该目录（见 Dockerfile），用户无需手动下载
    HF_HOME: str | None = None
    EMBEDDING_MODEL_PATH: str | None = None

    # 检索参数（默认值同时作为系统配置的默认值来源，见 routes/settings.py::_system_defaults）
    RECALL_TOP_K: int = 10  # 每路召回数量（送入 RRF 融合）
    RERANK_TOP_N: int = 3  # 精排后返回给 LLM 的最终数量
    RRF_K: int = 60  # RRF 融合平滑常数
    # 相关性门槛（BUSINESS_RULES §6：检索相关度 < 0.35 时拒答）
    RELEVANCE_THRESHOLD: float = 0.35
    # 阶段十四：Evidence Gate 总开关（False = 完全回到阶段十三及之前的行为）。
    # 默认开启；评测可用 use_evidence_gate 显式覆盖，用于"Gate 开 / 关"对照实验。
    EVIDENCE_GATE_ENABLED: bool = True
    # 阶段十五：Self Reflection 总开关（False = 完全回到阶段十四的行为）。
    # 默认开启；评测可用 use_self_reflection 显式覆盖，用于 ON/OFF 对照实验。
    SELF_REFLECTION_ENABLED: bool = True
    # 阶段十五：可选的 LLM Reflection（一致性检查）。默认关闭。
    # 说明：默认方案是纯规则反思（确定性、零额外延迟）；开启后每个答案额外调用
    # 一次 LLM，仅用于判断"答案是否被资料支持"，不得用于生成新事实/新结论。
    SELF_REFLECTION_LLM_ENABLED: bool = False
    # LLM Reflection 单次调用的超时上限（秒）；超时即视为不可用，回落到原答案
    SELF_REFLECTION_LLM_TIMEOUT_SECONDS: float = 20.0
    # flagreranker（BGE-Reranker-v2-m3 真实精排）
    RERANK_BACKEND: str = "flagreranker"
    RERANK_MODEL: str = "BAAI/bge-reranker-v2-m3"  # HuggingFace 模型 ID
    # 本地模型路径（镜像内置）：设了优先用本地路径，避免运行时下载
    # 为空则按 RERANK_MODEL 从 HuggingFace 拉取（国内走 HF_ENDPOINT 镜像）
    RERANK_MODEL_PATH: str | None = None
    # 设备：cpu / cuda:0 / mps；CPU 上自动禁用 fp16
    RERANK_DEVICE: str = "cpu"
    # HyDE 查询改写（TECH_DESIGN §4.5：检索前生成假设答案替换原问题）
    # 默认开启并复用主 LLM；关闭改动的开关是 HYDE_ENABLED=false
    HYDE_ENABLED: bool = True
    HYDE_BACKEND: str = "openai"  # openai（OpenAI 兼容，默认复用主 LLM）
    # 默认留空 = 复用主 LLM 的模型；需要独立小模型（如 Qwen2.5-1.5B）时再填
    HYDE_MODEL: str = ""
    # HyDE 小模型 API 地址：为空则复用 LLM_BASE_URL
    # Ollama：http://localhost:11434/v1（Ollama ≥0.1.x 兼容 OpenAI /v1）
    # vLLM  ：http://localhost:8001/v1（vllm serve Qwen/Qwen2.5-1.5B-Instruct --port 8001）
    HYDE_BASE_URL: str | None = None
    HISTORY_WINDOW: int = 5  # BUSINESS_RULES §6：默认携带最近 5 轮历史
    # 会话缓存（TECH_DESIGN：conversation:{conv_id}:history TTL 24h）
    HISTORY_TTL_SECONDS: int = 86400

    # 回收站
    TRASH_RETENTION_DAYS: int = 7  # BUSINESS_RULES §5：默认 7 天，可配置 1-30 天

    # 审计 IP 来源（BUG-072）：逗号分隔的可信反向代理 IP 列表，例如 "172.18.0.2,127.0.0.1"。
    # **留空（默认）时不解析 X-Forwarded-For**，行为与本改动前完全一致（直连 peer IP）。
    # 一旦填写，只有当直连 peer 命中白名单时才采用 XFF 的最左跳，
    # 避免客户端直接伪造 XFF 污染审计日志（审计 IP 是安全溯源依据）。
    TRUSTED_PROXY_IPS: str = ""

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

    # ── 真实知识数据导入中心（仅扫描本机目录，不接收浏览器大文件上传）────────
    # 数据源根目录，必须经 .env 配置，禁止写死在业务代码中
    IMPORT_SOURCE_DIR: str = ""
    # 单个数据集预览/扫描时最多读取的记录数（防止 1GB 级语料拖垮接口）
    IMPORT_SCAN_SAMPLE_ROWS: int = 5
    # 超大文件（如 JSON 语料）记录数采用"按已读字节估算"而非全量扫描的阈值（MB）
    IMPORT_ESTIMATE_THRESHOLD_MB: int = 64
    # 单次导入任务允许的最大记录数（安全阀：禁止一次全量导入百万级语料）
    IMPORT_MAX_RECORDS_PER_JOB: int = 200

    # 开发环境默认管理员（系统首次启动时若 users 表为空则自动创建）
    DEFAULT_ADMIN_EMAIL: str = "admin@company.com"
    DEFAULT_ADMIN_USERNAME: str = "admin"
    DEFAULT_ADMIN_PASSWORD: str = "admin123456"

    # 评估质量门禁（AGENTS.md：answer_correctness ≥ 0.75 才允许合入）
    EVAL_ACCURACY_THRESHOLD: float = 0.75

    @model_validator(mode="after")
    def _check_production_secret(self) -> "Settings":
        """生产环境禁止使用内置默认密钥（否则任何人都能签发管理员 JWT）。"""
        if self.ENV == "prod" and self.SECRET_KEY == DEV_DEFAULT_SECRET_KEY:
            raise ValueError(
                "生产环境（ENV=prod）必须通过环境变量 SECRET_KEY 配置独立随机密钥，"
                "禁止使用内置默认值"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

# 注入 HuggingFace 下载相关环境变量（huggingface_hub 在首次下载时读取）。
# 必须在 FlagEmbedding/transformers 触发下载前设置；config 在启动早期被各模块导入。
os.environ.setdefault("HF_ENDPOINT", settings.HF_ENDPOINT)
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", str(settings.HF_HUB_DOWNLOAD_TIMEOUT))
if settings.HF_HOME:
    os.environ.setdefault("HF_HOME", settings.HF_HOME)
    os.environ.setdefault("HUGGINGFACE_HUB_CACHE", settings.HF_HOME)
