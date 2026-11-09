"""应用配置：所有敏感信息通过 .env 注入，禁止硬编码。"""

import os
import re
from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 仅用于本地开发/测试的默认密钥；生产环境必须经环境变量 SECRET_KEY 覆盖，
# 否则启动即失败（BUG-026：固定密钥可被用于伪造 JWT）。
DEV_DEFAULT_SECRET_KEY = "hfimJesB4aztr41rt3zgnBWKgyY7VIgq5C0LmNoUCSHnUCicRImiHt-_wDpmmbYN"
# .env.example / 常见教程占位值：生产环境同样必须拒绝，否则“复制模板”即可上线弱密钥
WEAK_SECRET_KEYS = frozenset({
    "change-me-in-production",
    "change-me",
    "changeme",
    "secret",
    "password",
    "admin",
})

# 支持的环境名称（大小写与首尾空白不敏感）。
# production 归一化为 prod，避免下游 settings.ENV == "prod" 判断被绕过；
# 未知值直接拒绝，避免拼写错误（如 prodution）被当作非生产环境放行。
ENV_DEV = "dev"
ENV_TEST = "test"
ENV_TESTING = "testing"
ENV_PROD = "prod"
ENV_PRODUCTION = "production"
ENV_ALIASES = {ENV_PRODUCTION: ENV_PROD}
SUPPORTED_ENVS = frozenset({ENV_DEV, ENV_TEST, ENV_TESTING, ENV_PROD, ENV_PRODUCTION})

# 开发 / 示例默认凭据：生产环境只按字段拒绝，错误信息不回显具体值
DEV_DEFAULT_DB_PASSWORD = "postgres"
DEV_DEFAULT_MINIO_ACCESS_KEY = "minioadmin"
DEV_DEFAULT_MINIO_SECRET_KEY = "minioadmin"
DEV_DEFAULT_ADMIN_PASSWORD = "admin123456"

# 生产环境服务/管理员密码黑名单（大小写不敏感）
WEAK_PASSWORDS = frozenset({
    "postgres",
    "password",
    "changeme",
    "change-me",
    "secret",
    "admin",
    "admin123456",
    "minioadmin",
    "root",
    "123456",
    "12345678",
})

MIN_PROD_DB_PASSWORD_LENGTH = 12
MIN_PROD_MINIO_SECRET_LENGTH = 12
MIN_PROD_ADMIN_PASSWORD_LENGTH = 12

# 明显占位符标记：归一化（小写、去除非字母数字）后做子串匹配，
# 因此 CHANGE_ME、change me、CHANGE-ME-ACCESS、Your_Password 等都能识别。
PLACEHOLDER_MARKERS = (
    "changeme",
    "replaceme",
    "replacethis",
    "yourpassword",
    "yoursecret",
    "youraccesskey",
    "yourkey",
    "placeholder",
    "dummy",
    "examplepassword",
    "examplesecret",
    "examplekey",
)

# Compose 把 POSTGRES_* 原样拼进 DATABASE_URL，不做百分号编码。
# 主机名只接受常规字符；出现 @ 等字符说明密码未转义并污染了主机部分。
SAFE_DB_HOST = re.compile(r"^[A-Za-z0-9._-]+$")
URL_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")


def _looks_like_placeholder(value: str) -> bool:
    """判断是否为明显占位符（忽略大小写、首尾空白与分隔符）。"""
    normalized = re.sub(r"[^a-z0-9]", "", (value or "").lower())
    return bool(normalized) and any(marker in normalized for marker in PLACEHOLDER_MARKERS)


def _database_url_problem(
    database_url: str,
    expected_user: str | None,
    expected_password: str | None,
    expected_database: str | None,
) -> str | None:
    """检查 Compose 直接拼接 DATABASE_URL 时的 URL 字符兼容性。

    返回问题描述（不含具体凭据），无问题时返回 None。Compose 只做原样插入：
    - 密码含 "@" 会截断密码并把 "@..." 混入主机；
    - "%XX" 会被 make_url() 解码，与实际数据库密码不一致。
    POSTGRES_* 可见时逐项比对解析结果与原始值；不可见时退回结构检查。
    """
    from sqlalchemy.engine import make_url

    try:
        parsed = make_url(database_url)
    except Exception:
        return "DATABASE_URL 必须可解析"

    host = parsed.host or ""
    if not host or not SAFE_DB_HOST.fullmatch(host):
        return (
            "DATABASE_URL 主机部分异常，用户名或密码可能包含未转义的 "
            "@ 等 URL 保留字符"
        )

    if expected_user is not None and (parsed.username or "") != expected_user:
        return "DATABASE_URL 用户名与 POSTGRES_USER 不一致（必须使用 URL 安全字符）"
    if expected_password is not None and (parsed.password or "") != expected_password:
        return (
            "DATABASE_URL 密码与 POSTGRES_PASSWORD 不一致"
            "（Compose 拼接不支持 @ / % 等 URL 保留字符）"
        )
    if expected_database is not None and (parsed.database or "") != expected_database:
        return "DATABASE_URL 库名与 POSTGRES_DB 不一致（必须使用 URL 安全字符）"

    # 缺少原始密码值时无法判断 %XX 是否被解码，生产环境保守拒绝。
    if expected_password is None and URL_PERCENT_ESCAPE.search(database_url):
        return "DATABASE_URL 含 URL 百分号转义，无法确认与真实数据库密码一致"
    return None


def _database_password(database_url: str) -> str | None:
    """返回 DATABASE_URL 中的明文密码；无法解析时返回 None。

    SQLAlchemy 的 make_url() 已对 userinfo 完成一次 URL 解码，这里不能再调用
    unquote()：密码中的字面量 "%2F"（URL 里写作 %252F）会被二次解码成 "/"，
    使弱口令判定与实际连接密码不一致。异常信息不携带 URL，避免泄露凭据。
    """
    from sqlalchemy.engine import make_url

    try:
        parsed = make_url(database_url)
    except Exception:
        return None
    return parsed.password or ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # 校验失败时不回显输入值，避免启动日志/pytest 输出泄露真实凭据
        hide_input_in_errors=True,
    )

    # 应用
    APP_NAME: str = "Knowledge Platform API"
    VERSION: str = "0.1.0"
    ENV: str = "dev"  # dev / test / testing / prod / production（production 归一为 prod）
    API_PREFIX: str = "/api/v1"
    CORS_ORIGINS: list[str] = ["http://localhost:3000"]
    LOG_LEVEL: str = "INFO"

    # 数据库 / 缓存 / 向量库
    DATABASE_URL: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/knowledge_platform"
    )
    # 仅用于生产环境校验 Compose 拼接后 DATABASE_URL 的一致性（Compose 会经
    # env_file 把 .env 中的同名变量传入）；未提供时退回 URL 结构检查。
    POSTGRES_USER: str | None = None
    POSTGRES_PASSWORD: str | None = None
    POSTGRES_DB: str | None = None
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
    # 模型内部真正的前向微批（一次前向编码多少条）：实测 2 线程 CPU 上
    # 16 已是最优，放大到 32/64 反而明显变慢（长序列的内存/缓存压力更大）。
    EMBEDDING_BATCH_SIZE: int = 16
    MILVUS_DIM: int = 1024
    # Milvus 单次 insert RPC 发送的行数（结构化向量化与分批写入共用）。
    # 只影响 RPC 分片，不改变写入总量；保持较小值避免单条 gRPC 消息过大。
    VECTORIZE_BATCH_SIZE: int = 64
    # 一次 Embedding 调用传入的文本条数（外层批次），也是进度回写粒度。
    # 放大后 FlagEmbedding 能在更大集合内按长度排序再切微批，减少动态
    # padding 的填充；模型内部前向仍按 EMBEDDING_BATCH_SIZE 控制，单条
    # 由 max_length=1024 截断，因此不抬高单次前向的 token 上限。
    VECTORIZE_ENCODE_BATCH_SIZE: int = 256

    # ── 结构化数据解析 / 关联记录生成的分批大小 ──────────────────────────────
    # 解析：每批从文件产出多少条记录、每次 INSERT 多少条；关联生成：每次从
    # structured_records 读多少条锚点、每次 INSERT 多少条关联记录。
    # 批次只限制内存峰值，不限制业务总行数（不再有 2 万条硬上限）。
    STRUCTURED_PARSE_READ_BATCH: int = 2000
    STRUCTURED_PARSE_WRITE_BATCH: int = 1000
    STRUCTURED_GROUP_READ_BATCH: int = 2000
    STRUCTURED_GROUP_WRITE_BATCH: int = 500
    # 结构化向量化：每批从 PG 读多少条记录（读一批 → 编码一批 → 写一批）。
    # 内存峰值 ≈ 该值 × 单条记录向量大小，不随文件总行数增长；
    # 批内 Embedding / Milvus 发送仍按 VECTORIZE_BATCH_SIZE 细分。
    STRUCTURED_VECTOR_READ_BATCH: int = 500
    # 关联知识生成的最大跳数：从锚点沿已确认关系最多展开几跳（A→B 为 1 跳，
    # A→B→C 为 2 跳）。默认 2；调大可支持更长链式关联，循环与重复路径会自动停止。
    # 只影响正式关联生成，不影响关系配置与校验。
    STRUCTURED_RELATION_MAX_HOPS: int = 2

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
    # HyDE 查询改写（TECH_DESIGN §4.5：检索前生成假设答案，作补充召回；原问题始终保留）
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
    # 宿主机数据目录（前端展示 / 备份说明；容器内实际路径为 UPLOAD_DIR）
    HOST_DATA_DIR: str = "./docker-data"
    MAX_FILE_SIZE_MB: int = 50  # BUSINESS_RULES §4：默认 50MB（可配置）

    # 解析 / 切片（TECH_DESIGN：结构感知切片，512~1024 tokens，overlap 50~100）
    PARSE_BACKEND: str = "background"  # background（进程内线程池）/ celery（Redis 队列）
    CHUNK_SIZE_TOKENS: int = 768
    CHUNK_OVERLAP_TOKENS: int = 100
    CHUNK_MIN_TOKENS: int = 32

    # 文件存储：local（本地磁盘）/ minio（S3 兼容对象存储）
    STORAGE_BACKEND: str = "local"
    MINIO_ENDPOINT: str = "localhost:9000"
    MINIO_ACCESS_KEY: str = DEV_DEFAULT_MINIO_ACCESS_KEY
    MINIO_SECRET_KEY: str = DEV_DEFAULT_MINIO_SECRET_KEY
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
    DEFAULT_ADMIN_PASSWORD: str = DEV_DEFAULT_ADMIN_PASSWORD

    # 评估质量门禁（AGENTS.md：answer_correctness ≥ 0.75 才允许合入）
    EVAL_ACCURACY_THRESHOLD: float = 0.75

    @field_validator("ENV", mode="before")
    @classmethod
    def _normalize_env(cls, value: object) -> str:
        """归一化并校验环境名称，未知值直接拒绝。

        支持 dev / test / testing / prod / production（大小写与首尾空白不敏感）；
        production 归一化为 prod，保证下游对 settings.ENV 的比较（启动种子、
        测试模式、破坏性操作门禁）行为一致，不会因别名写法绕过生产限制。
        """
        env = str(value or "").strip().lower()
        if env not in SUPPORTED_ENVS:
            raise ValueError(
                "ENV 配置非法：仅支持 dev / test / testing / prod / production"
            )
        return ENV_ALIASES.get(env, env)

    @model_validator(mode="after")
    def _check_production_security(self) -> "Settings":
        """生产环境拒绝示例密钥、默认服务凭据和不完整的管理员初始化配置。

        ENV 已在字段校验阶段归一化为 prod（含 production 别名），错误信息只包含
        字段名和规则，不回显字段值；配合 hide_input_in_errors 防止启动日志输出
        真实凭据。

        生产启动不会自动执行 init_db()，管理员字段仅服务于一次性初始化；
        这里仍做保守校验，避免后续手工种子或误切回 dev 时使用默认密码。
        """
        if self.ENV != ENV_PROD:
            return self

        problems: list[str] = []

        secret = (self.SECRET_KEY or "").strip()
        if (
            secret.lower() == DEV_DEFAULT_SECRET_KEY.lower()
            or secret.lower() in WEAK_SECRET_KEYS
            or _looks_like_placeholder(secret)
            or len(secret) < 32
        ):
            problems.append(
                "SECRET_KEY 必须为独立随机值（至少 32 字符，禁止内置默认值或占位值）"
            )

        db_problem = _database_url_problem(
            self.DATABASE_URL,
            self.POSTGRES_USER,
            self.POSTGRES_PASSWORD,
            self.POSTGRES_DB,
        )
        if db_problem:
            problems.append(db_problem)
        else:
            db_password = _database_password(self.DATABASE_URL)
            # _database_url_problem 已保证 URL 可解析
            if not db_password:
                problems.append("DATABASE_URL 必须包含非空密码")
            elif (
                db_password.strip().lower() in WEAK_PASSWORDS
                or _looks_like_placeholder(db_password)
                or len(db_password) < MIN_PROD_DB_PASSWORD_LENGTH
            ):
                problems.append(
                    "PostgreSQL 密码必须为非默认值、非占位值且至少 "
                    f"{MIN_PROD_DB_PASSWORD_LENGTH} 字符"
                )

        # 即使当前 STORAGE_BACKEND=local 也校验：Compose 中的 MinIO 仍服务于 Milvus，
        # 且后续切换对象存储时不应重新引入默认凭据。
        minio_access = (self.MINIO_ACCESS_KEY or "").strip()
        minio_secret = (self.MINIO_SECRET_KEY or "").strip()
        if (
            not minio_access
            or minio_access.lower() in WEAK_PASSWORDS
            or _looks_like_placeholder(minio_access)
        ):
            problems.append("MINIO_ACCESS_KEY 必须为非默认值、非占位值")
        if (
            not minio_secret
            or minio_secret.lower() in WEAK_PASSWORDS
            or _looks_like_placeholder(minio_secret)
            or len(minio_secret) < MIN_PROD_MINIO_SECRET_LENGTH
        ):
            problems.append(
                "MINIO_SECRET_KEY 必须为非默认值、非占位值且至少 "
                f"{MIN_PROD_MINIO_SECRET_LENGTH} 字符"
            )

        admin_email = (self.DEFAULT_ADMIN_EMAIL or "").strip()
        admin_username = (self.DEFAULT_ADMIN_USERNAME or "").strip()
        admin_password = (self.DEFAULT_ADMIN_PASSWORD or "").strip()
        if not admin_email or "@" not in admin_email:
            problems.append("DEFAULT_ADMIN_EMAIL 必须为非空合法邮箱")
        if not admin_username or any(ch.isspace() for ch in admin_username):
            problems.append("DEFAULT_ADMIN_USERNAME 必须为非空且不含空白字符")
        if (
            not admin_password
            or admin_password.lower() in WEAK_PASSWORDS
            or _looks_like_placeholder(admin_password)
            or len(admin_password) < MIN_PROD_ADMIN_PASSWORD_LENGTH
        ):
            problems.append(
                "DEFAULT_ADMIN_PASSWORD 必须为非默认值、非占位值且至少 "
                f"{MIN_PROD_ADMIN_PASSWORD_LENGTH} 字符"
            )

        if problems:
            raise ValueError("生产环境配置不安全：" + "；".join(problems))
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
