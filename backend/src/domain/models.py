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

from src.core.file_spaces import DOCUMENT_SPACE, ROOT_FOLDER_SENTINEL, STRUCTURED_SPACE


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
    # ── 文件管理（第一阶段：文件夹）───────────────────────────────────────
    # 所属空间：document = 普通文档（解析 + 向量化），structured = 结构化数据
    # （只做文件管理，不入库检索）。存量行由 server_default 补成 document，
    # 因此老数据与老页面行为完全不变，两个空间的列表天然不混。
    space: Mapped[str] = mapped_column(
        String(16),
        default=DOCUMENT_SPACE,
        server_default=DOCUMENT_SPACE,
        index=True,
    )
    # 所在文件夹：NULL = 空间根目录。移动文件只改这一列，storage_path 保持不变，
    # 所以整理目录不会重复上传、不会搬迁物理文件，也不可能丢文件。
    folder_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("file_folders.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    # ── 内容指纹（第五阶段：重复文件提示）────────────────────────────────────
    # 按原始字节 sha256，上传/替换时顺手算（内容本来已读进内存），NULL = 该列上线前的历史文件未检测。
    # 只用于「同一知识库里有相同内容的文件」这一条提示，不做模糊匹配，也不据此删除或覆盖任何数据。
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)


class FileFolder(Base, TimestampMixin):
    """文件文件夹树：普通文档与结构化数据各一棵，互不混用。

    设计要点：
    - ``parent_id`` 自引用实现层级；``ondelete=RESTRICT`` 让数据库层兜住误删父目录。
    - 同一 ``(space, parent_id)`` 下 ``name`` 唯一。根目录 parent_id 为 NULL，
      SQL 唯一约束对 NULL 不生效，故用 COALESCE 表达式索引归一到零 UUID
      （与 categories 表同一手法）。
    - 不做软删除/回收站：删除接口要求目录为空（无子文件夹、无文件），
      因此不存在「删掉的目录名占位」问题，也不引入多余状态机。
    - 权限沿用文档自身的 KB 权限：文件夹本身只是组织信息，不承载数据可见性，
      避免在本阶段过度设计一套目录 ACL。
    """

    __tablename__ = "file_folders"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    space: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str] = mapped_column(String(64))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("file_folders.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    __table_args__ = (
        Index(
            "uq_file_folders_space_parent_name",
            "space",
            text(f"coalesce(parent_id, '{ROOT_FOLDER_SENTINEL}')"),
            "name",
            unique=True,
        ),
    )

    parent: Mapped["FileFolder | None"] = relationship(
        back_populates="children", remote_side=[id]
    )
    children: Mapped[list["FileFolder"]] = relationship(
        back_populates="parent", passive_deletes=True
    )


# ── 结构化数据解析（第三阶段：非关联结构化数据）────────────────────────────────
# 只服务 space=structured 的文件：一张文件级解析概览 + N 条独立知识记录。
# 刻意与导入中心的 import_files/import_records/generic_entities 分开：那些表绑定
# import_jobs（按批次幂等、映射到业务表），语义与本阶段「每个文件独立解析」不符。
# 这里的 doc_id 外键用 CASCADE，所以删除文件时解析结果随之清理，不需要额外的级联代码。


STRUCTURED_PARSE_PENDING = "pending"
STRUCTURED_PARSE_PARSING = "parsing"
STRUCTURED_PARSE_SUCCESS = "success"
STRUCTURED_PARSE_FAILED = "failed"

# 第六阶段：结构化知识进入统一向量索引后的状态（与 documents.index_status 同口径）
VECTOR_STATUS_PENDING = "pending"
VECTOR_STATUS_INDEXING = "indexing"
VECTOR_STATUS_COMPLETED = "completed"
VECTOR_STATUS_FAILED = "failed"


class StructuredParse(Base, TimestampMixin):
    """一个结构化数据文件的解析结果概览（字段识别 + 记录统计）。

    状态机：pending → parsing → success / failed。
    ``failed`` 只更新本行的状态与错误信息，**不动旧记录**，所以新一次解析失败
    不会破坏上一份可用的解析结果。
    """

    __tablename__ = "structured_parses"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    # 一个文件一份解析结果；文件彻底删除时级联清理（回收站 purge / 过期清理）
    doc_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), unique=True, index=True
    )
    # 冗余 kb_id：与 chunks 同样做法，检索/权限校验无需回表 documents
    kb_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), default=STRUCTURED_PARSE_PENDING, index=True)
    # 识别出的实际格式：csv / tsv / xlsx / json（与 documents.file_type 可能不同名）
    detected_format: Mapped[str] = mapped_column(String(16), default="")
    # 文本类文件识别到的编码（utf-8 / gbk …），二进制格式留空
    encoding: Mapped[str] = mapped_column(String(32), default="")
    # 字段清单：[{"name","type","null_count","sheet","note"}]，完全来自文件本身，不预设
    fields: Mapped[list] = mapped_column(JSONB, default=list)
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    # 最近一次解析实际读取的数据行数（失败时是失败前已读取的行数）
    read_count: Mapped[int] = mapped_column(Integer, default=0)
    # 最近一次解析失败时未能写入的行数；成功恒为 0
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    # 整行为空 / 无法构成记录的行数
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    # 含空值或列数异常的记录数（预览页据此提示）
    anomaly_count: Mapped[int] = mapped_column(Integer, default=0)
    # 兼容旧数据/旧接口：当前没有业务行数上限，新解析恒为 False
    truncated: Mapped[bool] = mapped_column(default=False)
    error_message: Mapped[str] = mapped_column(Text, default="")
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 第六阶段：本文件的记录是否已写入统一向量索引（pending/indexing/completed/failed）
    vector_status: Mapped[str] = mapped_column(String(16), default=VECTOR_STATUS_PENDING)
    vector_count: Mapped[int] = mapped_column(Integer, default=0)
    vector_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    vector_error: Mapped[str] = mapped_column(Text, default="")
    vectorized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class StructuredRecord(Base, TimestampMixin):
    """一条独立知识记录：由文件中的一行（或 JSON 中一个对象）动态生成。

    - ``payload`` 原样保留字段与值：不转型、不翻译、不丢字段（类型推断只写在
      StructuredParse.fields 里，不改数据本身）。
    - ``knowledge_text`` 是按实际字段拼出的可读文本（如
      「名称：黄芪；性味：甘，微温。」），空值字段不参与拼接。
    - 来源追溯：doc_id / file_name（回表 documents）+ folder_id（回表目录链）
      + row_no / sheet_name / json_path 定位到原始行或 JSON 路径。
    - 不加唯一约束：每次成功解析是「整份替换」（同一事务内先删旧再插新），
      XLSX 多工作表的行号还会各自从 1 开始，(doc_id, row_no) 本身并不唯一。
    """

    __tablename__ = "structured_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    doc_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    kb_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    # 文件内的生成顺序（0 基）：分页预览按它排序。
    # 不能直接用 row_no 排序——XLSX 每个工作表的行号各自从 1 开始，按行号排会把
    # 不同工作表的行混在一起；seq_no 保证「所见顺序 = 文件顺序」。
    seq_no: Mapped[int] = mapped_column(Integer, default=0)
    # 原始行号（CSV/TSV/XLSX，1 基，含表头与空行的真实位置）
    row_no: Mapped[int] = mapped_column(Integer, default=0)
    sheet_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # JSON 定位路径（如 $[3]、$.herbs.黄芪）；CSV/TSV/XLSX 留空
    json_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # 记录在文件内的展示标签（JSON 顶层对象的键名等），不写进 payload
    record_label: Mapped[str] = mapped_column(String(255), default="")
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    knowledge_text: Mapped[str] = mapped_column(Text, default="")
    # 该记录的空值字段与异常说明：["字段「性味」为空", "列数少于表头"]
    anomalies: Mapped[list] = mapped_column(JSONB, default=list)

    __table_args__ = (
        # 预览按文件内原始顺序分页浏览
        Index("ix_structured_records_doc_seq", "doc_id", "seq_no"),
    )


# ── 关联结构化数据（第四阶段：多个文件之间的关系）──────────────────────────────
# 与第三阶段的边界：第三阶段「一个文件独立解析」，本阶段「多个文件作为一个完整文件组」
# 处理。两套表互不复用：structured_parses / structured_records 仍只描述单文件结果，
# 本阶段只**读取**它们（不重新解析文件，也不改动非关联数据的任何行为）。
#
# 与导入中心的 import_jobs / import_files / import_records 也刻意分开：那套表服务
# 「导入到业务表（herbs/prescriptions…）」，本阶段不写业务表，只生成关联知识记录。
#
# RAG 边界（本阶段硬约束）：这三张表只落 PostgreSQL，不调用 Embedding、不写 Milvus，
# 也不碰 documents.parse_status / chunks；统一向量化留到第六阶段。


STRUCTURED_GROUP_DRAFT = "draft"
STRUCTURED_GROUP_VALIDATED = "validated"
STRUCTURED_GROUP_CONFIRMED = "confirmed"
# 组内文件被替换或删除：既有正式结果不再可信，必须重新校验并由用户再次确认
STRUCTURED_GROUP_STALE = "stale"

# 关联类型（one_to_one / one_to_many / many_to_one / many_to_many）定义在
# src/utils/structured_relation.py，那里是唯一来源，避免两处常量漂移。


class StructuredGroup(Base, TimestampMixin):
    """一个关联数据文件组 = 一个独立的关联导入任务。

    状态机：draft（关系未确认）→ validated（已校验）→ confirmed（已生成正式记录）；
    组内文件替换/删除会把已确认的组打成 stale，stale 状态下不能直接算正式结果。

    - ``relations``：关系配置列表，每项含 from/to（doc_id + 字段）、kind、
      origin（suggested 推测 / manual 用户选择）、confirmed（用户是否确认）、
      confidence 与 reason、stats（匹配/未匹配/重复计数）。
      **推测结果默认 confirmed=False**，未经用户确认不参与正式生成。
    - ``validation``：最近一次校验报告（问题清单 + 每项关系的统计），只是快照，
      文件替换后置空，需重新校验。
    """

    __tablename__ = "structured_groups"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    kb_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(
        String(16), default=STRUCTURED_GROUP_DRAFT, index=True
    )
    relations: Mapped[list] = mapped_column(JSONB, default=list)
    validation: Mapped[dict] = mapped_column(JSONB, default=dict)
    # 正式关联记录数（仅 confirmed 时有意义；stale 保留数字供对比，页面按状态区分展示）
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str] = mapped_column(Text, default="")
    validated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 第六阶段：关联记录是否已写入统一向量索引（仅 confirmed 时才允许存在向量）
    vector_status: Mapped[str] = mapped_column(String(16), default=VECTOR_STATUS_PENDING)
    vector_count: Mapped[int] = mapped_column(Integer, default=0)
    vector_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    vector_error: Mapped[str] = mapped_column(Text, default="")
    vectorized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class StructuredGroupFile(Base, TimestampMixin):
    """文件组的一个成员文件（关系图的节点）。

    ``doc_id`` FK CASCADE：彻底删除文件时这行随之消失，路由层负责把该组打成 stale，
    所以不会留下指向不存在文件的关系（无效关系）。``alias`` 是用户给该文件起的角色名
    （如「学生表」「选课表」），只影响关联记录文本可读性，不改原始数据。
    """

    __tablename__ = "structured_group_files"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("structured_groups.id", ondelete="CASCADE"), index=True
    )
    doc_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    kb_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    # 组内顺序（决定关联记录里各文件字段的展示顺序）
    order_no: Mapped[int] = mapped_column(Integer, default=0)
    alias: Mapped[str] = mapped_column(String(64), default="")
    # 建组时的文件名快照：文件后来重命名/移目录，仍能看出当时用的是哪份数据
    file_name: Mapped[str] = mapped_column(String(255), default="")

    __table_args__ = (
        # 同一文件在一个文件组里只允许出现一次（避免重复关系与重复关联记录）
        UniqueConstraint("group_id", "doc_id", name="uq_structured_group_files_group_doc"),
    )


class StructuredGroupRecord(Base, TimestampMixin):
    """一条关联知识记录：由已确认关系把多个文件的记录连起来后生成。

    与第三阶段的 ``structured_records``（单文件一行一条）不同，这里一条记录可能横跨
    学生表 + 选课表 + 课程表，因此：

    - ``knowledge_text``：连贯的关系文本（如「学生：张三；选修课程：高等数学；成绩：90。」），
      不是把关系表某一行孤立翻译，也保留一对多 / 多对多的完整上下文。
    - ``parts``：各参与文件的原始字段与值（不改数据），用于详情展示。
    - ``sources``：每个参与记录的追溯坐标（doc_id / file_name / record_id / row_no /
      sheet_name / json_path / 参与关联的字段与值）。
    - ``anomalies``：未匹配、重复主键等提示；**异常记录同样生成记录**，只是带上提示，
      不为了「关联成功」而丢弃或修改原始记录。
    """

    __tablename__ = "structured_group_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("structured_groups.id", ondelete="CASCADE"), index=True
    )
    kb_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    seq_no: Mapped[int] = mapped_column(Integer, default=0)
    # 该条记录的主文件（关系图的起点）；文件被删时不阻塞删除，故不加外键
    anchor_doc_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    anchor_record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    knowledge_text: Mapped[str] = mapped_column(Text, default="")
    parts: Mapped[list] = mapped_column(JSONB, default=list)
    sources: Mapped[list] = mapped_column(JSONB, default=list)
    relation_ids: Mapped[list] = mapped_column(JSONB, default=list)
    anomalies: Mapped[list] = mapped_column(JSONB, default=list)

    __table_args__ = (
        Index("ix_structured_group_records_group_seq", "group_id", "seq_no"),
    )


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


# 实体/关系的确认状态词表：通用导入（GenericRelation 等）复用，
# 原知识图谱（KG）专用状态常量随 KG 功能下线仅保留此组仍在使用者
STATUS_CONFIRMED = "confirmed"
STATUS_PENDING = "pending"  # 候选、未经人工确认：不参与推理
STATUS_REJECTED = "rejected"


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
    # 候选置信度 0~1：低于阈值的候选保持 pending，未经人工确认不参与推理
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    # pending / confirmed / rejected
    status: Mapped[str] = mapped_column(String(16), default=STATUS_PENDING, index=True)
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
