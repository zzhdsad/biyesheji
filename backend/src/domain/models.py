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
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)  # 软删除时间（回收站，到期硬删）


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
    error_message: Mapped[str] = mapped_column(Text, default="")
    # 来源可信度标注（AGENTS.md 中医约束第 6 条）；历史文档可为空，经补标接口回填
    # source_type 受控枚举见 src.core.source_meta；credibility_level 由其固定映射推导
    source_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    era: Mapped[str | None] = mapped_column(String(8), nullable=True)  # 先秦/汉/唐/宋/明/清/现代
    credibility_level: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)  # 1-5
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)  # 回收站软删除时间


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
    __tablename__ = "test_cases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    kb_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    question: Mapped[str] = mapped_column(Text)
    golden_answer: Mapped[str] = mapped_column(Text)
    golden_contexts: Mapped[list | None] = mapped_column(JSONB, nullable=True)


class EvaluationResult(Base, TimestampMixin):
    __tablename__ = "evaluation_results"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    test_case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("test_cases.id", ondelete="CASCADE"), index=True
    )
    retrieved_contexts: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    context_relevancy: Mapped[float] = mapped_column(Float, default=0.0)
    answer_correctness: Mapped[float] = mapped_column(Float, default=0.0)


class Feedback(Base, TimestampMixin):
    __tablename__ = "feedbacks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), index=True
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
    llm_provider: Mapped[str] = mapped_column(String(32), default="mock")  # deepseek/openai/qwen/ollama/custom
    llm_base_url: Mapped[str] = mapped_column(String(512), default="")
    llm_model: Mapped[str] = mapped_column(String(128), default="")
    llm_api_key: Mapped[str] = mapped_column(String(512), default="")
    # Embedding
    embedding_backend: Mapped[str] = mapped_column(String(32), default="mock")  # mock/flagembedding
    embedding_model: Mapped[str] = mapped_column(String(128), default="BAAI/bge-m3")
    embedding_device: Mapped[str] = mapped_column(String(16), default="cpu")
    # Rerank
    rerank_backend: Mapped[str] = mapped_column(String(32), default="mock")  # mock/flagreranker
    rerank_model: Mapped[str] = mapped_column(String(128), default="BAAI/bge-reranker-v2-m3")
    rerank_device: Mapped[str] = mapped_column(String(16), default="cpu")
    # HyDE
    hyde_enabled: Mapped[bool] = mapped_column(default=True)
    hyde_backend: Mapped[str] = mapped_column(String(32), default="mock")  # mock/openai
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
    trash_retention_days: Mapped[int] = mapped_column(Integer, default=7)  # 回收站保留天数 1-30
    max_file_size_mb: Mapped[int] = mapped_column(Integer, default=50)  # 文件大小限制
    recall_top_k: Mapped[int] = mapped_column(Integer, default=50)  # 每路召回数量
    rerank_top_n: Mapped[int] = mapped_column(Integer, default=5)  # 精排后送 LLM 数量
    relevance_threshold: Mapped[float] = mapped_column(Float, default=0.3)  # 相似度拒答阈值
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

    __table_args__ = (
        Index("ix_literatures_aliases_gin", "aliases", postgresql_using="gin"),
    )

    # 与 Herb.tags / Prescription.tags / Theory.tags 同构：不建 back_populates，Tag 模型保持不变
    tags: Mapped[list[Tag]] = relationship(
        secondary=literature_tags, lazy="selectin"
    )
    # 分类对象（多对一），仅用于响应序列化；category_id 的 FK RESTRICT 不变
    category: Mapped[Category | None] = relationship(lazy="selectin")
