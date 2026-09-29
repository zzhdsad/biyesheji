"""embedding 模型标识与存量向量一致性判定。

背景
----
切换 embedding 模型（如 mock → BGE-M3）后，Milvus 中的存量向量仍是**旧模型**
生成的，与新模型的查询向量不在同一个向量空间，混在一起检索会得到错误结果。
本模块提供"模型标识 + 存量识别"的最小工具集，供：

- 向量化流程写入标识（documents.vector_model 与 Milvus 动态字段）
- 切换模型时把未标注（legacy）文档标记为旧模型
- 检索时排除旧模型向量（避免混检）
- 设置页展示"需要重新向量化的文档数量"

约定
----
model key = `backend:model`（**不含 device**：cpu/cuda 不影响向量语义）。
例：`flagembedding:BAAI/bge-m3`。

NULL 语义：本字段上线前已入库、未标注的文档为 NULL（legacy）。
- 检索**不排除** NULL（保持既有行为，升级不影响存量部署）
- 切换模型时统一标注为旧模型 → 变成"显式旧模型"后才进入统计与排除
"""

import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.domain.models import Document

# 认为"文档存在向量"的状态：切片就绪或已向量化（failed 也可能残留旧向量，
# 但重新向量化会先校验切片，故一并纳入待重建集合由任务自行判定）
VECTORIZED_STATUSES = ("success", "completed", "failed")


def model_key(backend: str | None, model: str | None) -> str:
    """由 backend + model 生成稳定的模型标识。"""
    b = (backend or settings.EMBEDDING_BACKEND).lower()
    m = model or settings.EMBEDDING_MODEL
    return f"{b}:{m}"


def key_from_config(config: dict | None) -> str:
    """由运行时配置（ModelConfig）生成当前生效的模型标识。"""
    c = config or {}
    return model_key(c.get("embedding_backend"), c.get("embedding_model"))


def _active_doc_filters() -> list:
    """文档"仍在使用中"的过滤条件：未软删除 + 所属知识库不在回收站。

    回收站知识库里的文档同样不应计入"需要重新向量化"——它们已从所有列表与
    检索入口移除，统计进去只会让设置页长期显示一个无法消除的告警。
    """
    from src.domain.models import KnowledgeBase

    trashed_kbs = select(KnowledgeBase.id).where(KnowledgeBase.deleted_at.is_not(None))
    return [
        Document.deleted_at.is_(None),
        Document.kb_id.not_in(trashed_kbs),
    ]


async def stale_doc_ids(
    db: AsyncSession,
    current_key: str,
    kb_id: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    """返回"已明确由其它模型生成向量"的文档 id。

    只统计**显式标注且不等于当前模型**的文档；NULL（legacy 未标注）不计入，
    以保证升级到本版本时检索行为与过去完全一致（不触发全量重建）。
    """
    stmt = select(Document.id).where(
        *_active_doc_filters(),
        Document.vector_model.is_not(None),
        Document.vector_model != current_key,
    )
    if kb_id is not None:
        stmt = stmt.where(Document.kb_id == kb_id)
    rows = (await db.scalars(stmt)).all()
    return list(rows)


async def stale_models(db: AsyncSession, current_key: str) -> list[str]:
    """列出当前库里存在的、与生效模型不一致的旧模型标识（用于前端展示）。"""
    stmt = (
        select(Document.vector_model)
        .where(
            *_active_doc_filters(),
            Document.vector_model.is_not(None),
            Document.vector_model != current_key,
        )
        .distinct()
    )
    rows = (await db.scalars(stmt)).all()
    return [r for r in rows if r]


async def pending_doc_ids(
    db: AsyncSession,
    current_key: str,
    kb_id: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    """需要重新向量化的文档：显式旧模型文档（按 id 排序，便于稳定分页）。"""
    return await stale_doc_ids(db, current_key, kb_id)


async def stamp_legacy_documents(
    db: AsyncSession,
    old_key: str,
    kb_id: uuid.UUID | None = None,
) -> int:
    """把"未标注"的存量文档标注为旧模型 old_key（切换模型时调用）。

    这样切到 BGE-M3 后，此前由旧模型生成的向量会被识别为"旧模型向量"：
    进入重新向量化统计，并在检索时被排除，避免新旧向量混检。

    Returns:
        被标注的文档数量。
    """
    stmt = (
        update(Document)
        .where(
            Document.deleted_at.is_(None),
            Document.vector_model.is_(None),
            Document.parse_status.in_(VECTORIZED_STATUSES),
        )
        .values(vector_model=old_key)
    )
    if kb_id is not None:
        stmt = stmt.where(Document.kb_id == kb_id)
    result = await db.execute(stmt)
    await db.commit()
    return int(result.rowcount or 0)
