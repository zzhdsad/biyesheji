"""PostgreSQL ORM 模型（SQLAlchemy 2.0 风格），与 TECH_DESIGN.md 数据模型对应。"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, String, Text, func
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
