"""索引一致性校验与修复（PostgreSQL ↔ Milvus）。

背景：历史上「文档是否已向量化」只有 documents.parse_status == 'completed' 一个
间接标记，且由写入方自行设置，与 Milvus 中真实存在的向量没有任何校验。结果：
标记成功但向量缺失（检索取不到）、文档已删但向量残留（孤儿向量污染检索）、
重复写入导致同一分片多条向量（重复召回）。

本服务把「PG 侧期望」与「Milvus 侧实际」分开计算，产出**可审查的差异报告**，
修复必须基于报告显式触发，且默认绝不删除任何向量。

安全约束（硬性）：
- diff() 只读，不修改任何 PG/Milvus 数据；
- apply() 默认只修正不准确的状态标记，**不删向量**；
- 删除孤儿/重复向量必须 delete_orphans/delete_duplicates=True **且** confirmed=True；
- 每次删除前后都会记录可审计日志（数量、文档、ID 前缀）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models import (
    INDEX_STATUS_FAILED,
    INDEX_STATUS_INDEXED,
    INDEX_STATUS_NOT_INDEXED,
    INDEX_STATUS_STALE,
    Chunk,
    Document,
)
from src.infrastructure.milvus_store import BaseVectorStore, get_vector_store

# 报告中最多保留的 ID 明细条数（避免超大报告）
MAX_ID_DETAIL = 50


@dataclass
class DocIndexDiff:
    """单个文档的索引差异。"""

    doc_id: str
    file_name: str
    kb_id: str
    expected_chunks: int
    actual_vectors: int
    missing_chunk_ids: list[str] = field(default_factory=list)
    orphan_vector_ids: list[str] = field(default_factory=list)
    duplicate_vector_ids: list[str] = field(default_factory=list)
    current_status: str = INDEX_STATUS_NOT_INDEXED
    suggested_status: str = INDEX_STATUS_NOT_INDEXED

    @property
    def flag_inaccurate(self) -> bool:
        return self.current_status != self.suggested_status

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "file_name": self.file_name,
            "kb_id": self.kb_id,
            "expected_chunks": self.expected_chunks,
            "actual_vectors": self.actual_vectors,
            "missing_count": len(self.missing_chunk_ids),
            "orphan_count": len(self.orphan_vector_ids),
            "duplicate_count": len(self.duplicate_vector_ids),
            "missing_chunk_ids": self.missing_chunk_ids[:MAX_ID_DETAIL],
            "orphan_vector_ids": self.orphan_vector_ids[:MAX_ID_DETAIL],
            "duplicate_vector_ids": self.duplicate_vector_ids[:MAX_ID_DETAIL],
            "current_status": self.current_status,
            "suggested_status": self.suggested_status,
            "flag_inaccurate": self.flag_inaccurate,
        }


@dataclass
class IndexDiffReport:
    """可审查的差异报告（diff() 的产物，apply() 的输入）。"""

    docs: list[DocIndexDiff] = field(default_factory=list)
    kb_id: str | None = None
    scanned_documents: int = 0
    milvus_available: bool = True
    error: str = ""

    @property
    def total_missing(self) -> int:
        return sum(len(d.missing_chunk_ids) for d in self.docs)

    @property
    def total_orphan(self) -> int:
        return sum(len(d.orphan_vector_ids) for d in self.docs)

    @property
    def total_duplicate(self) -> int:
        return sum(len(d.duplicate_vector_ids) for d in self.docs)

    @property
    def stale_flag_docs(self) -> list[DocIndexDiff]:
        return [d for d in self.docs if d.flag_inaccurate]

    @property
    def clean(self) -> bool:
        return (
            self.milvus_available
            and not self.total_missing
            and not self.total_orphan
            and not self.total_duplicate
            and not self.stale_flag_docs
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kb_id": self.kb_id,
            "scanned_documents": self.scanned_documents,
            "milvus_available": self.milvus_available,
            "error": self.error,
            "clean": self.clean,
            "totals": {
                "missing_vectors": self.total_missing,
                "orphan_vectors": self.total_orphan,
                "duplicate_vectors": self.total_duplicate,
                "inaccurate_flags": len(self.stale_flag_docs),
            },
            "documents": [d.to_dict() for d in self.docs],
        }


@dataclass
class ApplyResult:
    """修复结果（便于接口返回与测试断言）。"""

    flags_updated: int = 0
    orphans_deleted: int = 0
    duplicates_deleted: int = 0
    rebuild_marked: int = 0
    skipped_deletions: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "flags_updated": self.flags_updated,
            "orphans_deleted": self.orphans_deleted,
            "duplicates_deleted": self.duplicates_deleted,
            "rebuild_marked": self.rebuild_marked,
            "skipped_deletions": self.skipped_deletions,
        }


class IndexStateService:
    """索引状态校验与修复（只读 diff + 显式 apply）。"""

    def __init__(self, db: AsyncSession, store: BaseVectorStore | None = None) -> None:
        self.db = db
        self.store = store or get_vector_store()

    # ── 校验（只读）────────────────────────────────────────────────────────

    async def diff(
        self,
        *,
        kb_id: uuid.UUID | None = None,
        doc_ids: Sequence[uuid.UUID] | None = None,
    ) -> IndexDiffReport:
        """比对「PG 期望分片」与「Milvus 实际向量」，产出差异报告。

        本方法**不修改任何数据**：Milvus 不可用时返回带 error 的报告而不是抛错，
        便于运维只读巡检。
        """
        report = IndexDiffReport(kb_id=str(kb_id) if kb_id else None)

        documents = await self._load_documents(kb_id, doc_ids)
        report.scanned_documents = len(documents)
        if not documents:
            return report

        for doc in documents:
            expected = await self._chunk_ids(doc.id)
            try:
                rows = self.store.query_rows(str(doc.id))
            except Exception as exc:  # noqa: BLE001 - 只读巡检不应中断
                report.milvus_available = False
                report.error = f"Milvus 查询失败：{exc}"
                logger.warning(f"[index_state] Milvus 不可用，差异报告不完整: {exc}")
                return report

            actual_ids = [str(row.get("id")) for row in rows if row.get("id") is not None]
            seen: set[str] = set()
            duplicates: list[str] = []
            for vector_id in actual_ids:
                if vector_id in seen:
                    duplicates.append(vector_id)
                else:
                    seen.add(vector_id)

            expected_set = set(expected)
            missing = sorted(expected_set - seen)
            orphan = sorted(seen - expected_set)

            report.docs.append(
                DocIndexDiff(
                    doc_id=str(doc.id),
                    file_name=doc.file_name,
                    kb_id=str(doc.kb_id),
                    expected_chunks=len(expected),
                    actual_vectors=len(seen),
                    missing_chunk_ids=missing,
                    orphan_vector_ids=orphan,
                    duplicate_vector_ids=sorted(set(duplicates)),
                    current_status=doc.index_status,
                    suggested_status=self._suggest_status(
                        doc, expected_count=len(expected), actual_count=len(seen), missing=len(missing)
                    ),
                )
            )
        return report

    @staticmethod
    def _suggest_status(
        doc: Document, *, expected_count: int, actual_count: int, missing: int
    ) -> str:
        """根据 PG 期望与 Milvus 实际推算应有的索引状态。"""
        if doc.index_error:
            return INDEX_STATUS_FAILED
        if expected_count == 0:
            return INDEX_STATUS_NOT_INDEXED
        if actual_count == 0:
            return INDEX_STATUS_NOT_INDEXED
        if missing > 0:
            # 部分分片缺失：既不能算已索引，也不能算没索引
            return INDEX_STATUS_STALE
        return INDEX_STATUS_INDEXED

    async def _load_documents(
        self, kb_id: uuid.UUID | None, doc_ids: Sequence[uuid.UUID] | None
    ) -> list[Document]:
        query = select(Document).where(Document.deleted_at.is_(None))
        if kb_id is not None:
            query = query.where(Document.kb_id == kb_id)
        if doc_ids:
            query = query.where(Document.id.in_(list(doc_ids)))
        return list((await self.db.execute(query)).scalars().all())

    async def _chunk_ids(self, doc_id: uuid.UUID) -> list[str]:
        rows = await self.db.execute(
            select(Chunk.id).where(Chunk.doc_id == doc_id)
        )
        return [str(row[0]) for row in rows.all()]

    # ── 修复（显式触发）────────────────────────────────────────────────────

    async def apply(
        self,
        report: IndexDiffReport,
        *,
        fix_flags: bool = True,
        delete_orphans: bool = False,
        delete_duplicates: bool = False,
        confirmed: bool = False,
    ) -> ApplyResult:
        """按报告修复。**删除向量必须显式确认**。

        :param fix_flags: 修正 documents 上不准确的索引状态标记（安全，默认开）
        :param delete_orphans: 删除 Milvus 中无对应分片的孤儿向量
        :param delete_duplicates: 删除 Milvus 中重复的向量 ID
        :param confirmed: 执行任何删除前必须为 True
        :raises ValueError: 请求删除但未确认时
        """
        result = ApplyResult()
        wants_delete = delete_orphans or delete_duplicates
        if wants_delete and not confirmed:
            raise ValueError(
                "删除 Milvus 向量属不可逆操作：请先审查 diff() 报告，"
                "再以 confirmed=True 显式确认（orphans="
                f"{report.total_orphan}, duplicates={report.total_duplicate}）"
            )

        if fix_flags:
            result.flags_updated = await self._fix_flags(report)

        if delete_orphans:
            for doc_diff in report.docs:
                if not doc_diff.orphan_vector_ids:
                    continue
                result.orphans_deleted += self._safe_delete(
                    doc_diff.orphan_vector_ids, kind="孤儿向量", doc_id=doc_diff.doc_id
                )

        if delete_duplicates:
            for doc_diff in report.docs:
                if not doc_diff.duplicate_vector_ids:
                    continue
                result.duplicates_deleted += self._safe_delete(
                    doc_diff.duplicate_vector_ids, kind="重复向量", doc_id=doc_diff.doc_id
                )

        logger.info(f"[index_state] 修复完成: {result.to_dict()}")
        return result

    def _safe_delete(self, vector_ids: list[str], *, kind: str, doc_id: str) -> int:
        """按 ID 删除向量并记录审计日志（幂等）。"""
        if not vector_ids:
            return 0
        logger.warning(
            f"[index_state] 删除{kind} doc_id={doc_id} 数量={len(vector_ids)} "
            f"示例ID={vector_ids[:3]}"
        )
        self.store.delete_by_ids(vector_ids)
        return len(vector_ids)

    async def _fix_flags(self, report: IndexDiffReport) -> int:
        """把不准确的索引状态标记修正为报告建议值（只改 PG，不碰 Milvus）。"""
        updated = 0
        for doc_diff in report.stale_flag_docs:
            doc = await self.db.get(Document, uuid.UUID(doc_diff.doc_id))
            if doc is None:
                continue
            doc.index_status = doc_diff.suggested_status
            doc.vector_count = doc_diff.actual_vectors
            if doc_diff.suggested_status == INDEX_STATUS_INDEXED:
                doc.indexed_at = doc.indexed_at or datetime.now(timezone.utc)
            updated += 1
        if updated:
            await self.db.flush()
        return updated

    # ── 重建与失败重试 ─────────────────────────────────────────────────────

    async def mark_for_rebuild(
        self, kb_id: uuid.UUID | None = None, doc_ids: Sequence[uuid.UUID] | None = None
    ) -> int:
        """把待重建的文档重置为「未索引」，交由既有向量化流程重新入库。

        只重置状态、不直接编码：向量化逻辑保持在 IndexingService 单一实现，
        这里不复制一份，避免两条写入路径行为分叉。
        """
        report = await self.diff(kb_id=kb_id, doc_ids=doc_ids)
        marked = 0
        for doc_diff in report.docs:
            if not doc_diff.missing_chunk_ids:
                continue
            doc = await self.db.get(Document, uuid.UUID(doc_diff.doc_id))
            if doc is None:
                continue
            doc.index_status = INDEX_STATUS_NOT_INDEXED
            doc.index_error = ""
            marked += 1
        if marked:
            await self.db.flush()
        logger.info(f"[index_state] 已标记重建文档数={marked}")
        return marked

    async def retry_failed(self, kb_id: uuid.UUID | None = None) -> int:
        """清空失败文档的错误信息并重置为未索引，供向量化任务重试。"""
        query = select(Document).where(
            Document.deleted_at.is_(None),
            Document.index_status == INDEX_STATUS_FAILED,
        )
        if kb_id is not None:
            query = query.where(Document.kb_id == kb_id)
        documents = list((await self.db.execute(query)).scalars().all())
        for doc in documents:
            doc.index_status = INDEX_STATUS_NOT_INDEXED
            doc.index_error = ""
        if documents:
            await self.db.flush()
        logger.info(f"[index_state] 已重置失败文档数={len(documents)}")
        return len(documents)
