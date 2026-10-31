"""PostgreSQL ORM 模型（SQLAlchemy 2.0 风格），与 TECH_DESIGN.md 数据模型对应。"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _new_uuid() -> uuid.UUID:
    return uuid.uuid4()


class TimestampMixin:
    """创建/更新时间戳。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="member")  # admin / member / viewer
    name: Mapped[str] = mapped_column(String(64), default="")  # 姓名
    department: Mapped[str] = mapped_column(String(64), default="")  # 部门
    must_change_password: Mapped[bool] = mapped_column(default=False)  # 首次登录强制修改密码
    is_active: Mapped[bool] = mapped_column(default=True)  # 账号启用/禁用（离职=禁用，保留数据可恢复）
    # 线上库该列为 timestamptz（历史迁移 e6a3b8c1d2f4 调整过）。模型必须与库一致，
    # 否则每次 autogenerate 都会生成反向的类型变更，污染新迁移。
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # 软删除时间（回收站，到期硬删）


class KnowledgeBase(Base, TimestampMixin):
    __tablename__ = "knowledge_bases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(128), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    visibility: Mapped[str] = mapped_column(String(16), default="private")  # public / private
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)  # 回收站软删除时间

    members: Mapped[list["KBMember"]] = relationship(
        back_populates="knowledge_base", cascade="all, delete-orphan"
    )
    # TASK-008：挂载到该 KB 的传统资源（herb/prescription/theory/literature）。
    # 多态关联（resource_type + resource_id），不在此层建立到具体资源表的 FK；
    # 资源存在性由应用层在挂载/检索时校验。仅与 KnowledgeBaseResource 本层建关系。
    resources: Mapped[list["KnowledgeBaseResource"]] = relationship(
        back_populates="knowledge_base", cascade="all, delete-orphan"
    )


class KBMember(Base, TimestampMixin):
    __tablename__ = "kb_members"

    kb_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(16), default="viewer")  # owner / admin / editor / viewer

    knowledge_base: Mapped["KnowledgeBase"] = relationship(back_populates="members")


class KnowledgeBaseResource(Base):
    """KB ↔ 传统资源（Herb/Prescription/Theory/Literature）的多态挂载关联（TASK-008）。

    设计：
    - 多态关联：``resource_type`` 标识资源类型，``resource_id`` 指向对应资源表的主键；
      PostgreSQL 不支持单一 FK 指向多表，故 ``resource_id`` 不建 FK，
      资源存在性由应用层在挂载时校验（Stage 3 实现）。
    - 删除：``knowledge_base_id`` CASCADE（KB 删除时关联自动清理）；
      删除 Resource 时需应用层先清关联再删本体（DB 无法跨多态 CASCADE）。
    - 唯一约束：同一 KB 不允许重复挂载同一 Resource；不同 KB 可挂载同一 Resource。
    - ``created_at`` 单字段（无 ``updated_at``）：本表无业务字段更新需求，
      与纯关联表（herb_tags 等）一致；不引入 TimestampMixin 以免冗余 updated_at。
    """

    __tablename__ = "knowledge_base_resources"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE")
    )
    # resource_type 受控词表（herb/prescription/theory/literature）；
    # 与 User.role / Category.resource_type / Document.parse_status 风格一致，
    # 不引入 PostgreSQL ENUM，由应用层在 Stage 3 校验合法值。
    resource_type: Mapped[str] = mapped_column(String(16))
    # 多态关联：不建 FK；存在性/类型-UUID 匹配由应用层校验。
    resource_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    knowledge_base: Mapped["KnowledgeBase"] = relationship(back_populates="resources")

    __table_args__ = (
        # 同一 KB 内 (resource_type, resource_id) 唯一；不同 KB 可挂载同一资源。
        # 复合唯一约束自带以 knowledge_base_id 为最左前缀的索引，
        # 覆盖"KB 内全部挂载"查询，无需单独为 knowledge_base_id 建索引。
        UniqueConstraint(
            "knowledge_base_id",
            "resource_type",
            "resource_id",
            name="uq_kbr_kb_type_resource",
        ),
        # 反查索引：删除 Resource 时需按 (resource_type, resource_id) 清理所有挂载，
        # 唯一复合约束的最左前缀是 knowledge_base_id，不覆盖此查询路径，故单独建索引。
        Index(
            "ix_knowledge_base_resources_resource_lookup",
            "resource_type",
            "resource_id",
        ),
    )


# ── 向量索引状态（与 parse_status 解耦，用于可验证的索引状态管理）─────────────
# parse_status 只表示「切片是否就绪」；向量是否真的进了 Milvus 由 index_* 单独记录。
# 历史上二者混用导致「PG 有切片 = 已可检索」的误判（本库曾出现 16.7 万切片但
# Milvus 仅 3518 条向量的不一致），新字段必须由向量化流程按真实写入结果回填。
INDEX_STATUS_NOT_INDEXED = "not_indexed"
INDEX_STATUS_INDEXED = "indexed"
INDEX_STATUS_STALE = "stale"  # 内容已变更，与已有向量不一致，需重建
INDEX_STATUS_FAILED = "failed"

# ── 导入模式 ────────────────────────────────────────────────────────────────
IMPORT_MODE_NORMAL = "normal"  # 普通导入：无跨文件关联
IMPORT_MODE_RELATIONAL = "relational"  # 关联导入：含主外键/关联表


class Document(Base, TimestampMixin):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    kb_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    file_name: Mapped[str] = mapped_column(String(255))
    file_type: Mapped[str] = mapped_column(String(16))
    file_size: Mapped[int] = mapped_column(BigInteger, default=0)
    storage_path: Mapped[str] = mapped_column(String(512), default="")
    parse_status: Mapped[str] = mapped_column(String(16), default="pending")
    # pending / parsing / success（切片就绪）/ completed（已向量化）/ failed
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    # 历史迁移遗留列（c185a0406aa3）：当前业务不使用，但数据库 / Alembic 已存在
    progress_percent: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str] = mapped_column(Text, default="")
    # 来源可信度标注（AGENTS.md 中医约束第 6 条）；历史文档可为空，经补标接口回填
    # source_type 受控枚举见 src.core.source_meta；credibility_level 由其固定映射推导
    source_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    era: Mapped[str | None] = mapped_column(String(8), nullable=True)  # 先秦/汉/唐/宋/明/清/现代
    credibility_level: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)  # 1-5
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)  # 回收站软删除时间
    # 向量模型标识：记录"当前 Milvus 中该文档的向量由哪个 embedding 模型生成"。
    # 格式 `backend:model`（如 `flagembedding:BAAI/bge-m3`），由向量化流程写入。
    # NULL = 本字段上线前已入库、未标注（legacy）：检索时不排除（保持既有行为），
    # 但切换模型时会被统一标注为旧模型，从而进入"需重新向量化"统计。
    vector_model: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    # 导入溯源：来自哪个数据集、哪一批次（真实数据批次管理用，测试数据/手工录入为空）
    source_dataset: Mapped[str | None] = mapped_column(String(128), nullable=True)
    import_batch_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # ── Milvus 向量索引的真实状态（由向量化流程回填，可被独立校验）─────────────
    index_status: Mapped[str] = mapped_column(
        String(16), default=INDEX_STATUS_NOT_INDEXED, index=True
    )
    # 实际写入 Milvus 的向量条数（用于与 chunk_count 比对，发现漏写/孤儿向量）
    vector_count: Mapped[int] = mapped_column(Integer, default=0)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    index_error: Mapped[str] = mapped_column(Text, default="")


class ImportJob(Base, TimestampMixin):
    """真实知识数据导入任务（导入中心）。

    状态机：pending → processing → completed / failed（另可 cancelled）。
    只记录导入过程与结果，业务数据仍写入各自的业务表（herbs/prescriptions/
    theories/literatures/documents），并以 import_batch_id 标记批次。
    """

    __tablename__ = "import_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    # 批次标识：写入各业务行的 import_batch_id，用于统计/回滚某一次导入
    batch_id: Mapped[str] = mapped_column(String(64), index=True)
    # 数据集标识（scanner 生成的稳定 id）与展示名
    dataset_id: Mapped[str] = mapped_column(String(128))
    dataset_name: Mapped[str] = mapped_column(String(255), default="")
    # 具体文件（数据集为目录时为空）
    source_file: Mapped[str] = mapped_column(String(512), default="")
    # herb / prescription / theory / literature / document
    target_type: Mapped[str] = mapped_column(String(16), index=True)
    # 导入模式：normal（普通）/ relational（关联）。默认 normal，保证既有任务行为不变
    import_mode: Mapped[str] = mapped_column(String(16), default=IMPORT_MODE_NORMAL, index=True)
    # 导入 documents 时归属的知识库（资源类导入为空）
    kb_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    # pending / processing / completed / failed / cancelled
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    succeeded: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)  # 空值/重复被跳过
    # 失败明细：[{"index": n, "name": "...", "reason": "..."}]
    failed_items: Mapped[list] = mapped_column(JSONB, default=list)
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RevectorizeJob(Base, TimestampMixin):
    """批量重新向量化任务（切换 embedding 模型后重建存量向量）。

    任务只**重写 Milvus 中的向量**，绝不动 PostgreSQL 的文档 / 切片 / 资源业务数据；
    单篇文档失败只记入 failed_doc_ids，可整体重试，不影响其它文档。
    """

    __tablename__ = "revectorize_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    # 目标 embedding model key（本次重建要写入的模型）
    target_model: Mapped[str] = mapped_column(String(128))
    # 被替换掉的旧模型 key（用于展示"从 X 迁移到 Y"）
    previous_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 作用范围：None = 全库；否则仅该知识库
    kb_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    # pending / running / succeeded / partial / failed
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    succeeded: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    # 失败文档 id 列表（字符串），供"重试失败项"精确重建
    failed_doc_ids: Mapped[list] = mapped_column(JSONB, default=list)
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # 与 TimestampMixin 保持一致：带时区（迁移建的是 timestamptz，naive 会触发
    # asyncpg "can't subtract offset-naive and offset-aware" 错误）
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Chunk(Base, TimestampMixin):
    """文档切片（父子关联：Document 1→N Chunk），供溯源与后续向量化。"""

    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    doc_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    kb_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    # 与 Milvus document_chunks collection 字段对齐，便于同步（TECH_DESIGN 数据模型）
    page_num: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # 来源可信度冗余（自 documents 同步）：检索命中无需回表即可按来源分组/展示
    source_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    credibility_level: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)


class Conversation(Base, TimestampMixin):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(128), default="新会话")
    kb_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), default=list)


class Message(Base, TimestampMixin):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))  # user / assistant
    content: Mapped[str] = mapped_column(Text)
    citations: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class TestCase(Base, TimestampMixin):
    """评估测试集用例（TASK-009 扩展：问题分类 + 数据集版本 + 人工确认标记）。

    TASK-009 约束（AGENTS.md / PRD）：
    - 不凭空编造中医事实作为标准答案：golden_answer 为空或未经人工确认时，
      评估只计算 context_relevancy，answer_correctness 记为 NULL（不参与均值），
      避免把「未标注」误算成 0 分而污染实验结论。
    """

    __tablename__ = "test_cases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    kb_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    question: Mapped[str] = mapped_column(Text)
    golden_answer: Mapped[str] = mapped_column(Text)
    golden_contexts: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # 问题分类（受控词表见 application.evaluation_service.QUESTION_TYPES）：
    # herb/prescription/theory/literature/multi_source/unanswerable/general
    # 供 TASK-011 Query Analyzer / Dynamic Router 按问题类型对比检索策略效果。
    question_type: Mapped[str] = mapped_column(String(32), default="general", index=True)
    # 测试集版本（如 tcm-v1），实验对比时用于确认两次运行使用同一测试集
    dataset_version: Mapped[str] = mapped_column(String(64), default="v1", index=True)
    # 标准答案是否仍需人工确认：True → 不计算 answer_correctness
    needs_review: Mapped[bool] = mapped_column(default=True)
    # 人工标注依据提示（应依据哪条资源/文献核对），仅作标注指引，不参与打分
    source_reference: Mapped[str] = mapped_column(Text, default="")


class EvaluationRun(Base, TimestampMixin):
    """一次评估运行（实验）的归档记录。

    TASK-009：Baseline 实验能力。一次 run 固定「实验名 + 检索策略 + 测试集版本」，
    保存聚合指标与配置快照，使后续 Dynamic Router / 不同 Retrieval Strategy
    可以与 Baseline 在同一测试集下按指标和 question_type 进行对比。
    """

    __tablename__ = "evaluation_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    kb_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    experiment_name: Mapped[str] = mapped_column(String(128), default="baseline", index=True)
    # 检索策略标识（Baseline：hybrid_rrf_rerank_hyde）
    retrieval_strategy: Mapped[str] = mapped_column(String(64), default="baseline_hybrid")
    dataset_version: Mapped[str] = mapped_column(String(64), default="v1")
    case_count: Mapped[int] = mapped_column(Integer, default=0)
    # 有已确认标准答案、实际计算 answer_correctness 的用例数
    evaluated_count: Mapped[int] = mapped_column(Integer, default=0)
    # 标准答案缺失/待人工确认、跳过 answer_correctness 的用例数
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    # BUG-011：因基础设施故障（检索/生成抛异常）而未产出结果的用例数。
    # 这些用例不参与 context_relevancy / answer_correctness 均值，
    # 否则「Milvus 宕机」会被记成「检索质量 0 分」而污染实验结论。
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    # BUG-023：幂等键（调用方提供，如同一实验的重复/并发提交使用同一 key）。
    # NULL 表示未启用幂等保护；唯一索引保证同一 key 只会产生一条 run。
    idempotency_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True, unique=True
    )
    context_relevancy: Mapped[float] = mapped_column(Float, default=0.0)
    # 无已评估用例时为 NULL（区别于「全部答错=0」）
    answer_correctness: Mapped[float | None] = mapped_column(Float, nullable=True)
    passed: Mapped[bool] = mapped_column(default=False)
    threshold: Mapped[float] = mapped_column(Float, default=0.75)
    # 配置快照（模型/检索参数），保证实验可复现
    config_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # 按 question_type 分组的指标明细（[{question_type, case_count, evaluated_count,
    # context_relevancy, answer_correctness}]），支持按问题类型分析
    by_question_type: Mapped[list | None] = mapped_column(JSONB, nullable=True)


class EvaluationResult(Base, TimestampMixin):
    """单条用例的评估结果。

    TASK-009：补充 run_id / 实验维度冗余字段（experiment_name、retrieval_strategy、
    dataset_version、question_type）与生成答案、错误信息，便于按实验/按问题类型
    直接查询，无需回表 join。
    """

    __tablename__ = "evaluation_results"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    test_case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("test_cases.id", ondelete="CASCADE"), index=True
    )
    retrieved_contexts: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    context_relevancy: Mapped[float] = mapped_column(Float, default=0.0)
    # NULL 表示未评估（标准答案缺失或待人工确认），与 0 分（已评估但不正确）区分
    answer_correctness: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 历史迁移遗留列（c185a0406aa3）：当前指标计算不使用，保留以对齐 DB / Alembic
    faithfulness: Mapped[float] = mapped_column(Float, default=0.0)
    # 归属运行（evaluation_runs.id 的字符串形式，冗余存储便于分组查询）
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    experiment_name: Mapped[str] = mapped_column(String(128), default="baseline")
    retrieval_strategy: Mapped[str] = mapped_column(String(64), default="baseline_hybrid")
    dataset_version: Mapped[str] = mapped_column(String(64), default="v1")
    question_type: Mapped[str] = mapped_column(String(32), default="general")
    answer: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 阶段十四：Evidence Gate 归档（最小迁移，全部可空；Gate 关闭时为 None）
    # gate_decision ∈ {accept, insufficient, retry}
    gate_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    gate_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Gate 判定 retry 时实际重试使用的策略（未重试为 None）
    retry_strategy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 阶段十五：Self Reflection 归档（最小迁移，全部可空；Reflection 关闭时为 None）
    # reflection_decision ∈ {accept, revise, retry}
    reflection_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reflection_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Reflection 判定 retry 时实际使用的策略（未重试为 None）
    reflection_retry_strategy: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    # Reflection 给出的原因/问题清单（受控词表，+ 连接），供后续按原因归类分析
    reflection_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)


class Feedback(Base, TimestampMixin):
    """反馈（点赞/踩 + 纠错意见）。

    BUG-038：补充归属维度与唯一性：
    - user_id：反馈归属用户（可空，迁移前既有行无归属信息）；
    - (user_id, message_id) 唯一：同一用户对同一条消息只能保留一条反馈，
      由数据库兜底并发下的重复插入（应用层先查后写存在并发窗口）。
      PostgreSQL 唯一约束中 NULL 互不相等，历史行（user_id IS NULL）不受影响。
    """

    __tablename__ = "feedbacks"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "message_id", name="uq_feedbacks_user_message"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    rating: Mapped[int] = mapped_column(Integer)  # 1 赞 / -1 踩
    comment: Mapped[str] = mapped_column(Text, default="")


class ModelConfig(Base, TimestampMixin):
    """全局模型配置（单行表，id=1），用户在前端设置页动态配置。

    运行时优先读此表；为空时 fallback 到 .env 环境变量。
    """

    __tablename__ = "model_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    # LLM 生成
    llm_provider: Mapped[str] = mapped_column(String(32), default="openai")  # openai/deepseek/qwen/ollama/custom
    llm_base_url: Mapped[str] = mapped_column(String(512), default="")
    llm_model: Mapped[str] = mapped_column(String(128), default="")
    llm_api_key: Mapped[str] = mapped_column(String(512), default="")
    # Embedding（BGE-M3，模型权重随镜像内置）
    embedding_backend: Mapped[str] = mapped_column(String(32), default="flagembedding")
    embedding_model: Mapped[str] = mapped_column(String(128), default="BAAI/bge-m3")
    embedding_device: Mapped[str] = mapped_column(String(16), default="cpu")
    # Rerank（BGE-Reranker-v2-m3，模型权重随镜像内置）
    rerank_backend: Mapped[str] = mapped_column(String(32), default="flagreranker")
    rerank_model: Mapped[str] = mapped_column(String(128), default="BAAI/bge-reranker-v2-m3")
    rerank_device: Mapped[str] = mapped_column(String(16), default="cpu")
    # HyDE（默认开启；留空则复用主 LLM 的模型与地址）
    hyde_enabled: Mapped[bool] = mapped_column(default=True)
    hyde_backend: Mapped[str] = mapped_column(String(32), default="openai")
    hyde_model: Mapped[str] = mapped_column(String(128), default="")
    hyde_base_url: Mapped[str] = mapped_column(String(512), default="")


class AuditLog(Base):
    """审计日志：记录所有关键数据变更操作（用户管理、知识库增删改、文档增删改、问答、系统配置）。"""

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    operator_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    operator_name: Mapped[str] = mapped_column(String(64), default="")  # 冗余存储，用户删除后仍可追溯
    operation: Mapped[str] = mapped_column(String(64), index=True)  # create/update/delete/disable/restore/login 等
    target_type: Mapped[str] = mapped_column(String(32), index=True)  # user/kb/document/message/config
    target_id: Mapped[str] = mapped_column(String(64), default="", index=True)  # 目标对象 ID
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)  # 变更详情（前后值、额外参数）
    ip: Mapped[str] = mapped_column(String(64), default="")  # 操作来源 IP
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class SystemConfig(Base):
    """系统级配置（单行表，id=1）：回收站保留期、文件大小限制、检索参数等。

    运行时优先读此表；为空时 fallback 到 .env 环境变量。
    """

    __tablename__ = "system_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    # 以下默认值需与 core/config.py 中的 Settings 默认值保持一致，
    # 保证"新建行"与"无行回落"两条路径拿到同一套值。
    trash_retention_days: Mapped[int] = mapped_column(Integer, default=7)  # 回收站保留天数 1-30
    max_file_size_mb: Mapped[int] = mapped_column(Integer, default=50)  # 文件大小限制
    recall_top_k: Mapped[int] = mapped_column(Integer, default=10)  # 每路召回数量
    rerank_top_n: Mapped[int] = mapped_column(Integer, default=3)  # 精排后送 LLM 数量
    relevance_threshold: Mapped[float] = mapped_column(Float, default=0.35)  # 相似度拒答阈值
    history_window: Mapped[int] = mapped_column(Integer, default=5)  # 多轮对话历史轮数


class Category(Base, TimestampMixin):
    """通用分类：一张表按 resource_type 区分 4 棵独立树。

    resource_type 受控枚举：herb / prescription / theory / literature。
    parent_id 自引用实现层级（不引入闭包表）；自引用 FK ondelete=RESTRICT，
    有子节点时数据库层同样阻止删除。
    """

    __tablename__ = "categories"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    resource_type: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str] = mapped_column(String(64))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    description: Mapped[str] = mapped_column(Text, default="")
    # 软删除（回收站）：NULL = 正常；非空 = 已移入回收站，列表/检索不可见
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    # 同一 resource_type + 同一父节点下 name 唯一。
    # 普通 unique 约束对 parent_id IS NULL 的根节点不生效（SQL NULL 语义），
    # 故用 COALESCE 表达式唯一索引，保证同域根节点重名也被阻止。
    __table_args__ = (
        Index(
            "uq_categories_type_parent_name",
            "resource_type",
            text("coalesce(parent_id, '00000000-0000-0000-0000-000000000000')"),
            "name",
            unique=True,
        ),
    )

    parent: Mapped["Category | None"] = relationship(
        back_populates="children", remote_side=[id]
    )
    children: Mapped[list["Category"]] = relationship(
        back_populates="parent", passive_deletes=True
    )


class Tag(Base, TimestampMixin):
    """标签：扁平结构、跨资源域共享、name 全局唯一。不设 resource_type / 层级。"""

    __tablename__ = "tags"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    color: Mapped[str] = mapped_column(String(16), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    # 软删除（回收站）：与 Category / 四类资源同构
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


# 中药 ↔ 标签 多对多关联表（TASK-003）。
# - herb_id CASCADE：删除中药时关联行自动清理
# - tag_id RESTRICT：标签仍被中药引用时数据库层阻止删除（应用层 409 之外的兜底）
herb_tags = Table(
    "herb_tags",
    Base.metadata,
    Column(
        "herb_id",
        UUID(as_uuid=True),
        ForeignKey("herbs.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "tag_id",
        UUID(as_uuid=True),
        ForeignKey("tags.id", ondelete="RESTRICT"),
        primary_key=True,
    ),
)


class Herb(Base, TimestampMixin):
    """中药资源（TASK-003）。

    分类通过 category_id 多对一关联 categories（resource_type='herb' 的树节点）；
    标签通过 herb_tags 与 Tag 多对多。不使用 JSONB、不做软删除/多态关联。
    """

    __tablename__ = "herbs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(String(128)), default=list
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    properties: Mapped[str] = mapped_column(String(255), default="")  # 性味，如“苦，寒”
    channels: Mapped[list[str]] = mapped_column(
        ARRAY(String(32)), default=list
    )  # 归经，如 ['肺经', '胃经']
    effects: Mapped[str] = mapped_column(Text, default="")  # 功效
    source: Mapped[str] = mapped_column(String(255), default="")  # 出处/基原
    description: Mapped[str] = mapped_column(Text, default="")
    # 导入溯源（真实数据批次；手工录入为 NULL）
    source_dataset: Mapped[str | None] = mapped_column(String(128), nullable=True)
    import_batch_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # 软删除（回收站）：NULL = 正常；非空 = 已移入回收站，列表与检索不可见
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    __table_args__ = (
        Index("ix_herbs_aliases_gin", "aliases", postgresql_using="gin"),
        Index("ix_herbs_channels_gin", "channels", postgresql_using="gin"),
    )

    # selectin 预加载，避免异步会话中隐式懒加载触发 MissingGreenlet；
    # 不建 back_populates，Tag 模型保持不变
    tags: Mapped[list[Tag]] = relationship(
        secondary=herb_tags, lazy="selectin"
    )
    # 分类对象（多对一），仅用于响应序列化；category_id 的 FK RESTRICT 不变
    category: Mapped[Category | None] = relationship(lazy="selectin")


# 方剂 ↔ 标签 多对多关联表（TASK-004），与 herb_tags 同构。
# - prescription_id CASCADE：删除方剂时关联行自动清理
# - tag_id RESTRICT：标签仍被方剂引用时数据库层阻止删除
prescription_tags = Table(
    "prescription_tags",
    Base.metadata,
    Column(
        "prescription_id",
        UUID(as_uuid=True),
        ForeignKey("prescriptions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "tag_id",
        UUID(as_uuid=True),
        ForeignKey("tags.id", ondelete="RESTRICT"),
        primary_key=True,
    ),
)


class Prescription(Base, TimestampMixin):
    """方剂资源（TASK-004）。

    分类通过 category_id 多对一关联 categories（resource_type='prescription'
    的树节点）；标签通过 prescription_tags 与 Tag 多对多；组成（药材+用量）
    通过 prescription_ingredients 关联表表达，不使用 JSONB。
    """

    __tablename__ = "prescriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(String(128)), default=list
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    efficacy: Mapped[str] = mapped_column(Text, default="")  # 功效
    indications: Mapped[str] = mapped_column(Text, default="")  # 主治
    description: Mapped[str] = mapped_column(Text, default="")  # 方解/综合描述
    usage_method: Mapped[str] = mapped_column(String(255), default="")  # 用法，如“水煎服，每日一剂”
    source: Mapped[str] = mapped_column(String(255), default="")  # 出处，如《伤寒论》
    # 导入溯源（真实数据批次；手工录入为 NULL）
    source_dataset: Mapped[str | None] = mapped_column(String(128), nullable=True)
    import_batch_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # 软删除（回收站）：与 Herb 同构
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    __table_args__ = (
        Index("ix_prescriptions_aliases_gin", "aliases", postgresql_using="gin"),
    )

    # 组成：association object（含载荷列，不能用 secondary）。
    # selectin 预加载规避异步隐式懒加载；passive_deletes 依赖 DB CASCADE；
    # order_by 保证按 sort_order 稳定输出（君臣佐使顺序）
    ingredients: Mapped[list["PrescriptionIngredient"]] = relationship(
        cascade="all, delete-orphan",
        order_by="PrescriptionIngredient.sort_order",
        passive_deletes=True,
        lazy="selectin",
    )
    # 与 Herb.tags 同构：不建 back_populates，Tag 模型保持不变
    tags: Mapped[list[Tag]] = relationship(
        secondary=prescription_tags, lazy="selectin"
    )
    # 分类对象（多对一），仅用于响应序列化；category_id 的 FK RESTRICT 不变
    category: Mapped[Category | None] = relationship(lazy="selectin")


class PrescriptionIngredient(Base):
    """方剂组成行（TASK-004）：方剂 ↔ 中药 的带载荷关联。

    同一方剂不允许重复同一味中药（UniqueConstraint 兜底，API 层提前 400）；
    herb_id RESTRICT：中药仍被方剂引用时数据库层阻止删除。
    """

    __tablename__ = "prescription_ingredients"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    prescription_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("prescriptions.id", ondelete="CASCADE"), index=True
    )
    herb_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("herbs.id", ondelete="RESTRICT"), index=True
    )
    amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)  # 用量；NULL=“适量”类
    unit: Mapped[str] = mapped_column(String(16), default="")  # 单位，如“克”“两”
    processing: Mapped[str] = mapped_column(String(255), default="")  # 炮制/特殊处理，如“炙”“炒”
    role: Mapped[str] = mapped_column(String(32), default="")  # 君臣佐使等角色
    sort_order: Mapped[int] = mapped_column(Integer, default=0)  # 组成顺序

    __table_args__ = (
        UniqueConstraint(
            "prescription_id",
            "herb_id",
            name="uq_prescription_ingredients_prescription_herb",
        ),
    )

    # 中药对象（多对一），序列化组成时需要药名；selectin 预加载规避 MissingGreenlet
    herb: Mapped[Herb] = relationship(lazy="selectin")
    # 反向引用不预加载：响应序列化不访问 .prescription，
    # 且避免与 Prescription.ingredients 的 selectin 形成循环预加载；
    # viewonly=True：写入只经 ingredients 关系，消除双写冲突（SAWarning qzyx）
    prescription: Mapped["Prescription"] = relationship(viewonly=True)


# 中医理论 ↔ 标签 多对多关联表（TASK-005），与 herb_tags / prescription_tags 同构。
# - theory_id CASCADE：删除理论时关联行自动清理
# - tag_id RESTRICT：标签仍被理论引用时数据库层阻止删除
theory_tags = Table(
    "theory_tags",
    Base.metadata,
    Column(
        "theory_id",
        UUID(as_uuid=True),
        ForeignKey("theories.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "tag_id",
        UUID(as_uuid=True),
        ForeignKey("tags.id", ondelete="RESTRICT"),
        primary_key=True,
    ),
)


class Theory(Base, TimestampMixin):
    """中医理论资源（TASK-005）。

    分类通过 category_id 多对一关联 categories（resource_type='theory' 的树节点）；
    标签通过 theory_tags 与 Tag 多对多。不使用 JSONB、不做软删除/多态关联。
    """

    __tablename__ = "theories"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(String(128)), default=list
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    content: Mapped[str] = mapped_column(Text, default="")  # 核心正文
    source: Mapped[str] = mapped_column(String(255), default="")  # 来源出处
    # 导入溯源（真实数据批次；手工录入为 NULL）
    source_dataset: Mapped[str | None] = mapped_column(String(128), nullable=True)
    import_batch_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # 软删除（回收站）：与 Herb 同构
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    __table_args__ = (
        Index("ix_theories_aliases_gin", "aliases", postgresql_using="gin"),
    )

    # 与 Herb.tags / Prescription.tags 同构：不建 back_populates，Tag 模型保持不变
    tags: Mapped[list[Tag]] = relationship(
        secondary=theory_tags, lazy="selectin"
    )
    # 分类对象（多对一），仅用于响应序列化；category_id 的 FK RESTRICT 不变
    category: Mapped[Category | None] = relationship(lazy="selectin")


# 中医文献 ↔ 标签 多对多关联表（TASK-006），与 herb_tags / theory_tags 同构。
# - literature_id CASCADE：删除文献时关联行自动清理
# - tag_id RESTRICT：标签仍被文献引用时数据库层阻止删除
literature_tags = Table(
    "literature_tags",
    Base.metadata,
    Column(
        "literature_id",
        UUID(as_uuid=True),
        ForeignKey("literatures.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "tag_id",
        UUID(as_uuid=True),
        ForeignKey("tags.id", ondelete="RESTRICT"),
        primary_key=True,
    ),
)


class Literature(Base, TimestampMixin):
    """中医文献资源（TASK-006）：著作级文献条目的著录元数据 + 人工整理正文。

    分类通过 category_id 多对一关联 categories（resource_type='literature' 的树节点）；
    标签通过 literature_tags 与 Tag 多对多。不使用 JSONB、不做软删除/多态关联。
    与 Document（KB 内文件 + 解析/向量化流水线）相互独立，不设外键关联；
    资源进入 RAG 的衔接由后续 TASK-008 处理，本模型不承载任何向量/文件字段。
    """

    __tablename__ = "literatures"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(String(128)), default=list
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    author: Mapped[str] = mapped_column(String(255), default="")  # 作者（自由文本）
    dynasty: Mapped[str] = mapped_column(String(32), default="")  # 成书年代（自由文本，如“东汉”）
    summary: Mapped[str] = mapped_column(String(500), default="")  # 内容摘要
    content: Mapped[str] = mapped_column(Text, default="")  # 正文/精选段落
    source: Mapped[str] = mapped_column(String(255), default="")  # 版本/底本/出版依据
    # 导入溯源（真实数据批次；手工录入为 NULL）
    source_dataset: Mapped[str | None] = mapped_column(String(128), nullable=True)
    import_batch_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # 软删除（回收站）：与 Herb 同构
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    __table_args__ = (
        Index("ix_literatures_aliases_gin", "aliases", postgresql_using="gin"),
    )

    # 与 Herb.tags / Prescription.tags / Theory.tags 同构：不建 back_populates，Tag 模型保持不变
    tags: Mapped[list[Tag]] = relationship(
        secondary=literature_tags, lazy="selectin"
    )
    # 分类对象（多对一），仅用于响应序列化；category_id 的 FK RESTRICT 不变
    category: Mapped[Category | None] = relationship(lazy="selectin")


# ── 阶段十三：知识图谱（KG）───────────────────────────────────────────────────
# 定位：KG 是「现有资源 → 图谱节点/关系」的派生物，不是第二套中医知识库。
# 节点只保存定位与匹配所需的最小信息（类型 / 资源 ID / 名称 / 别名），
# 不复制资源正文；关系只保存能由现有业务数据可靠推导的那几种，
# 每条边记录 provenance（由哪张业务表/哪条规则生成），禁止凭空生成医学关系。

# 节点类型：目前只有资源节点（Herb/Prescription/Theory/Literature）
KG_NODE_TYPE_RESOURCE = "resource"
KG_NODE_TYPES: tuple[str, ...] = (KG_NODE_TYPE_RESOURCE,)

# 关系类型受控词表（第一版只实现有实际用途、且能可靠推导的关系）
KG_RELATION_CONTAINS = "contains"      # 方剂 → 组成 → 中药（prescription_ingredients）
KG_RELATION_RECORDS = "records"        # 文献 → 记载 → 资源（资源 source 命中文献名/别名）
KG_RELATION_RELATED_TO = "related_to"  # 跨类型资源共享标签（*_tags 关联表）

KG_RELATIONS: tuple[str, ...] = (
    KG_RELATION_CONTAINS,
    KG_RELATION_RECORDS,
    KG_RELATION_RELATED_TO,
)

KG_RELATION_LABELS: dict[str, str] = {
    KG_RELATION_CONTAINS: "组成",
    KG_RELATION_RECORDS: "记载",
    KG_RELATION_RELATED_TO: "相关",
}

# 关系来源（provenance）：记录边由哪张业务表 / 哪条规则生成，便于审计与实验
KG_PROVENANCE_INGREDIENT = "prescription_ingredients"
KG_PROVENANCE_SOURCE = "resource_source"
KG_PROVENANCE_SHARED_TAG = "shared_tag"


# ── 知识图谱：节点类型与关系/实体的确认状态 ────────────────────────────────────
KG_NODE_TYPE_ENTITY = "entity"
KG_STATUS_CONFIRMED = "confirmed"
KG_STATUS_PENDING = "pending"  # 候选、未经人工确认：不参与检索与推理
KG_STATUS_REJECTED = "rejected"


class KgNode(Base, TimestampMixin):
    """知识图谱节点：一个资源 = 一个节点；通用导入后一个实体 = 一个节点。

    - node_type：'resource'（业务资源） / 'entity'（通用导入实体）
    - resource_type + resource_id：指回 Herb / Prescription / Theory / Literature
      本体（多态，不建 FK，与 KnowledgeBaseResource 同风格）；实体节点下为 NULL
    - entity_id：指向 generic_entities；资源节点下为 NULL
    - kb_id：**必须隔离**。图谱遍历 / 检索一律按 kb_id 过滤，防止跨知识库关联泄漏
    - status：confirmed / pending / rejected。候选实体默认 pending，未确认不入检索
    - name / aliases：仅用于图检索的实体匹配，不复制资源正文
    - 唯一性改用**部分索引**：资源节点按 (resource_type, resource_id)，
      实体节点按 (entity_id)，两类节点共存且各自不重复
    """

    __tablename__ = "kg_nodes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    node_type: Mapped[str] = mapped_column(String(16), default=KG_NODE_TYPE_RESOURCE, index=True)
    kb_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=True, index=True
    )
    resource_type: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # 通用导入实体（node_type='entity' 时非空）
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    aliases: Mapped[list[str]] = mapped_column(ARRAY(String(128)), default=list)
    # 确认状态与候选置信度：低置信度候选默认 pending，确认后才 confirmed
    status: Mapped[str] = mapped_column(String(16), default=KG_STATUS_CONFIRMED, index=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        # 资源节点唯一（与原约束等价，改为部分索引以允许实体节点共存）
        Index(
            "uq_kg_nodes_resource",
            "resource_type",
            "resource_id",
            unique=True,
            postgresql_where=text("resource_type IS NOT NULL"),
        ),
        Index(
            "uq_kg_nodes_entity",
            "entity_id",
            unique=True,
            postgresql_where=text("entity_id IS NOT NULL"),
        ),
        Index("ix_kg_nodes_aliases_gin", "aliases", postgresql_using="gin"),
        # 按类型查节点（构建/统计路径）
        Index("ix_kg_nodes_type_resource_type", "node_type", "resource_type"),
        # 权限隔离 + 未确认不参与遍历
        Index("ix_kg_nodes_kb_status", "kb_id", "status"),
    )


class KgEdge(Base, TimestampMixin):
    """知识图谱边（TASK-013）：两个节点之间的一条可靠关系。

    - 关系以「规范方向」存储（如 方剂 → contains → 中药），检索时按无向遍历，
      不重复存储反向边
    - 唯一约束 (source_node_id, target_node_id, relation_type)：重复构建不产生重复边
    - provenance：边的生成依据（业务表 / 规则），description：可读说明（如用量、出处）
    - 节点删除时边级联删除，保证重建与资源清理不留孤儿边
    """

    __tablename__ = "kg_edges"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    source_node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kg_nodes.id", ondelete="CASCADE"), index=True
    )
    target_node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kg_nodes.id", ondelete="CASCADE"), index=True
    )
    relation_type: Mapped[str] = mapped_column(String(32), index=True)
    provenance: Mapped[str] = mapped_column(String(64), default="")
    description: Mapped[str] = mapped_column(String(255), default="")
    # 知识库隔离：图谱遍历必须按 kb_id 过滤，防止跨知识库关系泄漏
    kb_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # 确认状态：候选边默认 pending，未经人工确认不参与检索与推理
    status: Mapped[str] = mapped_column(String(16), default=KG_STATUS_CONFIRMED, index=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "source_node_id",
            "target_node_id",
            "relation_type",
            name="uq_kg_edges_source_target_relation",
        ),
        Index(
            "ix_kg_edges_relation_source", "relation_type", "source_node_id"
        ),
        Index("ix_kg_edges_kb_status", "kb_id", "status"),
    )


# ============================================================================
# 通用知识导入（普通导入与关联导入共用）
#
# 设计要点：
# - 复用既有 ImportJob 作为任务机，不另起 sessions 表；
# - import_files 记录文件夹递归的相对路径，支持同名文件区分；
# - import_records 保留原始 Schema 的每一条记录，不退化成文本块（决策 6）；
# - generic_entities / generic_relations 支持任意 Schema，不强制映射四类业务表；
# - 关系与实体一律带 kb_id，保证知识库隔离（决策 7）。
# ============================================================================


class ImportFile(Base, TimestampMixin):
    """导入文件清单：一次导入任务下的每个文件（含文件夹递归得到的相对路径）。"""

    __tablename__ = "import_files"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("import_jobs.id", ondelete="CASCADE"), index=True
    )
    # 文件夹递归导入时保留原始相对路径：区分「文件名相同但路径不同」的文件
    relative_path: Mapped[str] = mapped_column(String(512), default="")
    file_name: Mapped[str] = mapped_column(String(255), default="")
    file_ext: Mapped[str] = mapped_column(String(16), default="")
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    # 内容 sha256：用于重复文件识别与记录幂等
    file_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 自动识别的编码（utf-8 / gbk 等）；识别失败留空，由解析阶段给出明确错误
    encoding: Mapped[str] = mapped_column(String(32), default="")
    # 识别后的规范格式：csv/tsv/json/jsonl/xlsx/parquet/pdf/docx/txt/md
    detected_format: Mapped[str] = mapped_column(String(16), default="", index=True)
    # 结构化文件的记录数（文档类为 0）
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    # pending / parsed / imported / failed / skipped
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    error_message: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (
        UniqueConstraint("job_id", "relative_path", name="uq_import_files_job_path"),
    )


class ImportRecord(Base, TimestampMixin):
    """原始结构化记录：导入的每一行原文，保留完整字段，不拼接成文本块。

    record_key = sha256(file_hash:row_no) 是**稳定的源记录标识**：
    同一份文件重复导入时 record_key 不变，据此实现导入幂等（决策 7）。
    """

    __tablename__ = "import_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("import_jobs.id", ondelete="CASCADE"), index=True
    )
    file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("import_files.id", ondelete="CASCADE"), nullable=True, index=True
    )
    kb_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # 行号（1 基），与文件内位置对应，便于溯源到「哪个文件第几行」
    row_no: Mapped[int] = mapped_column(Integer)
    record_key: Mapped[str] = mapped_column(String(64), index=True)
    # 原始行的完整字段：不做类型转换、不丢字段、不翻译
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    # 该行被映射成的实体类型（可为空 = 未映射）
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    __table_args__ = (
        # 同一任务内同一源记录只存一份
        Index(
            "uq_import_records_job_record_key", "job_id", "record_key", unique=True
        ),
        # 同一知识库内同一源记录只存一份：重复导入天然幂等
        Index(
            "uq_import_records_kb_record_key",
            "kb_id",
            "record_key",
            unique=True,
            postgresql_where=text("kb_id IS NOT NULL"),
        ),
        Index("ix_import_records_kb_entity", "kb_id", "entity_type"),
    )


class GenericEntity(Base, TimestampMixin):
    """通用实体：任意 Schema 的导入实体，不强制映射到中药/方剂/理论/文献四类表。"""

    __tablename__ = "generic_entities"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    kb_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    # 用户/模板定义的类型标签，如 compound / target / disease / 中药 / 方剂
    entity_type: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(255), default="", index=True)
    # 原始字段全量保留（不丢字段、不改类型、不翻译）
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    # 溯源：来自哪个文件的哪一行
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("import_files.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_row_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 与 import_records.record_key 一致的稳定标识（用于幂等 upsert）
    record_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # 原始主键值：关系连接用，保留原样不做猜测
    source_pk: Mapped[str] = mapped_column(String(255), default="", index=True)
    import_batch_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        Index(
            "uq_generic_entities_kb_type_record_key",
            "kb_id",
            "entity_type",
            "record_key",
            unique=True,
            postgresql_where=text("record_key IS NOT NULL"),
        ),
        Index("ix_generic_entities_kb_type", "kb_id", "entity_type"),
        # 关系解析时按 (kb, 类型, 原始主键) 定位实体
        Index("ix_generic_entities_kb_pk", "kb_id", "entity_type", "source_pk"),
    )


class GenericRelation(Base, TimestampMixin):
    """通用关系：实体之间的关联。**必须带 kb_id**，保证知识库隔离与权限控制。"""

    __tablename__ = "generic_relations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    kb_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    source_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("generic_entities.id", ondelete="CASCADE"), index=True
    )
    target_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("generic_entities.id", ondelete="CASCADE"), index=True
    )
    relation_type: Mapped[str] = mapped_column(String(64), index=True)
    # 关系载荷（如用量、角色、来源字段值）
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    # 候选置信度 0~1：低于阈值的候选保持 pending，未经人工确认不进入 KG
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    # pending / confirmed / rejected
    status: Mapped[str] = mapped_column(String(16), default=KG_STATUS_PENDING, index=True)
    # 关系来源：发现规则 + 文件 + 字段，供审计与回溯
    provenance: Mapped[str] = mapped_column(String(255), default="")
    import_batch_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "kb_id",
            "source_entity_id",
            "target_entity_id",
            "relation_type",
            name="uq_generic_relations_kb_src_dst_type",
        ),
        Index("ix_generic_relations_kb_status", "kb_id", "status"),
        Index("ix_generic_relations_kb_type", "kb_id", "relation_type"),
    )


class ImportMappingTemplate(Base, TimestampMixin):
    """导入映射模板：相同 Schema 的数据集可复用字段映射与关系定义。

    schema_fingerprint = sha256(有序字段名列表 的规范化表示)。
    """

    __tablename__ = "import_mapping_templates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    # 归属知识库；为空表示全局模板
    kb_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(128), index=True)
    schema_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    # 模板内容：字段映射 / 实体类型 / 主键 / 关系定义
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("name", "schema_fingerprint", name="uq_import_mapping_name_fp"),
    )
