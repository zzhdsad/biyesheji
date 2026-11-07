"""结构化数据解析服务（第三阶段：非关联结构化数据）。

职责：读取 ``space=structured`` 的文件 → 识别字段结构 → 生成一条一条独立知识记录，
并把概览（字段清单、记录数、异常统计）落到 structured_parses / structured_records。

与 RAG 的关系：
- 解析本身**不调用 Embedding、不写 Milvus、不产生 chunks 切片**；普通文档的
  解析流水线（ParseService / IndexingService）与本服务完全不相交。
- 第六阶段：解析成功并提交后，再调用 ``sync_structured_document`` 把每条记录
  接入统一向量索引（复用第二阶段 Embedding + Milvus）。向量化失败只落
  vector_status=failed，不影响已经落库的解析结果，旧向量也不会被提前清空。
- 文件的 ``documents.parse_status`` 保持 ``skipped``：这是第二阶段确立的隔离标记
  （检索侧只认 completed、批量重建只认 success/completed/failed），本服务用
  structured_parses.status 表达结构化解析自己的进度，不改那两个语义。

失败保护：解析按批产出，但「删旧记录 + 分批插新记录 + 回写概览」始终在**同一个
事务**里。删除只在事务内可见，任何一批失败都整体回滚，旧的一份解析结果原样保留；
只有全部批次写完并提交成功，概览才会标记 success。解析中途读取/写入的行数会
记录到 read_count，失败时写清已读取与回滚的行数。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger
from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.exceptions import AppException, NotFoundError
from src.core.file_spaces import STRUCTURED_SPACE
from src.domain.models import (
    STRUCTURED_PARSE_FAILED,
    STRUCTURED_PARSE_PARSING,
    STRUCTURED_PARSE_PENDING,
    STRUCTURED_PARSE_SUCCESS,
    Document,
    StructuredParse,
    StructuredRecord,
)
from src.utils.structured_data import (
    StructuredParseError,
    StructuredRecordStream,
    StructuredRow,
    open_structured_stream,
)

_MAX_ERROR_CHARS = 2000


def _now() -> datetime:
    """timestamptz 列必须写带时区的时间（与 indexing_service 同一写法）。"""
    return datetime.now(timezone.utc)


# 后台任务崩溃（进程被杀）会把状态永久卡在 parsing；超过这个时长就视为残留，允许重新解析
_STALE_PARSING_MINUTES = 30


def _is_stale_parsing(row: StructuredParse) -> bool:
    stamp = row.updated_at or row.created_at
    if stamp is None:
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp < _now() - timedelta(minutes=_STALE_PARSING_MINUTES)


def _next_batch(stream: StructuredRecordStream) -> list[StructuredRow] | None:
    """在线程里取下一批；用 None 表示正常结束（StopIteration 不能穿过 await）。"""
    try:
        return next(stream)
    except StopIteration:
        return None


class StructuredParseService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_or_create(self, doc: Document) -> StructuredParse:
        """取该文件的解析概览行；还没有解析过时补一行 pending。"""
        row = await self.db.scalar(select(StructuredParse).where(StructuredParse.doc_id == doc.id))
        if row is not None:
            return row
        row = StructuredParse(doc_id=doc.id, kb_id=doc.kb_id, status=STRUCTURED_PARSE_PENDING)
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def run(self, doc_id: uuid.UUID) -> StructuredParse:
        doc = await self.db.get(Document, doc_id)
        if doc is None:
            raise NotFoundError("文档不存在")
        if doc.space != STRUCTURED_SPACE:
            raise AppException(
                400,
                "该文件属于普通文档空间，请走文档解析流程（结构化解析只处理结构化数据空间的文件）",
            )
        if doc.deleted_at is not None:
            raise AppException(400, "文件已在回收站中，无法解析")

        row = await self.get_or_create(doc)
        if row.status == STRUCTURED_PARSE_PARSING and not _is_stale_parsing(row):
            raise AppException(409, "该文件正在解析中，请稍后刷新查看结果")
        row_id = row.id
        # 状态由服务自己置为 parsing 并提交：路由派发时无需再改状态，
        # 重复点击会被上面的守卫拦住（超过 _STALE_PARSING_MINUTES 的残留 parsing 视为崩溃，允许重来）
        row.status = STRUCTURED_PARSE_PARSING
        await self.db.commit()
        await self.db.refresh(row)

        # 1) 读盘 + 分批解析/写库：删旧与插新在同一个事务里，未提交前旧记录仍有效
        read_count = 0
        written_count = 0
        anomaly_count = 0
        batch_count = 0
        stream: StructuredRecordStream | None = None
        try:
            stream = await self._open_stream(doc)
            await self.db.execute(
                delete(StructuredRecord).where(StructuredRecord.doc_id == doc.id)
            )
            seq_no = 0
            while True:
                batch = await asyncio.to_thread(_next_batch, stream)
                if batch is None:
                    break
                batch_count += 1
                read_count += len(batch)
                anomaly_count += sum(1 for item in batch if item.anomalies)
                write_size = max(1, settings.STRUCTURED_PARSE_WRITE_BATCH)
                for start in range(0, len(batch), write_size):
                    chunk = batch[start : start + write_size]
                    await self._insert_rows(doc, seq_no, chunk)
                    seq_no += len(chunk)
                    written_count += len(chunk)

            summary = stream.summary()
            if summary.read_count == 0:
                raise StructuredParseError("未识别到可写入的数据记录")
            # 只有全部批次写完才把概览标记 success；提交前任一步失败都会回滚
            row.status = STRUCTURED_PARSE_SUCCESS
            row.detected_format = summary.detected_format
            row.encoding = summary.encoding or ""
            row.fields = list(summary.fields)
            row.record_count = written_count
            row.read_count = summary.read_count
            row.failed_count = 0
            row.skipped_count = summary.skipped_count
            row.anomaly_count = anomaly_count
            row.truncated = False
            row.error_message = ""
            row.parsed_at = _now()
            await self.db.commit()
            await self.db.refresh(row)
        except Exception as exc:  # noqa: BLE001 - 任一批失败整份回滚，旧结果保留
            await self.db.rollback()
            if stream is not None:
                summary = stream.summary()
                read_count = max(read_count, summary.read_count)
            message = (
                f"第 {batch_count + 1} 批处理失败（本次已读取 {read_count} 行，"
                f"全部回滚，原有解析结果已保留）：{exc}"
            )
            logger.warning(f"结构化解析失败 doc_id={doc_id}: {exc}")
            return await self._mark_failed(
                row_id,
                message,
                read_count=read_count,
                failed_count=read_count,
            )
        finally:
            if stream is not None:
                stream.close()

        logger.info(
            f"结构化解析完成 doc_id={doc_id} 格式={row.detected_format} "
            f"字段={len(row.fields or [])} 记录={written_count} "
            f"批次={batch_count} 跳过行={row.skipped_count}"
        )

        # 2) 成员文件内容已变化：受影响的文件组必须重新校验/确认。
        #    只改状态和原因，关系配置与既有正式记录都保留，等用户确认后整份替换。
        try:
            await self._mark_groups_stale(doc.id)
        except Exception:  # noqa: BLE001 - 标记失败不能把已经成功的解析改回 failed
            logger.exception(f"结构化解析成功后标记文件组失效失败 doc_id={doc_id}")

        # 3) 解析事实已提交，再把该文件放进向量化队列（置 pending 后立即返回）。
        #    Embedding 由单消费者线程串行执行，多个文件同时解析完成也不会并发推理；
        #    入队失败不抛异常，旧向量继续有效，状态可由重新向量化接口重试。
        from src.application.structured_vector_service import sync_structured_document

        await sync_structured_document(self.db, doc.id)
        await self.db.refresh(row)
        return row

    async def _open_stream(self, doc: Document) -> StructuredRecordStream:
        from src.infrastructure.storage import get_storage

        storage = get_storage()
        if not doc.storage_path:
            raise AppException(400, "文件没有可读取的物理路径")
        content = await asyncio.to_thread(storage.load, doc.storage_path)
        # 只把 bytes 读进内存；记录由流式迭代器按批产出，不再一次性展开整份列表
        return await asyncio.to_thread(
            open_structured_stream,
            doc.file_type,
            content,
            max(1, settings.STRUCTURED_PARSE_READ_BATCH),
        )

    async def _mark_groups_stale(self, doc_id: uuid.UUID) -> None:
        from src.application.structured_group_service import StructuredGroupService

        await StructuredGroupService(self.db).mark_stale_by_docs(
            [doc_id],
            "成员文件已重新解析，关联结果需要重新校验并确认后再生成",
            drop_records=False,
        )
        await self.db.commit()

    async def _insert_rows(
        self, doc: Document, start_seq: int, rows: list[StructuredRow]
    ) -> None:
        if not rows:
            return
        values: list[dict[str, Any]] = [
            {
                "doc_id": doc.id,
                "kb_id": doc.kb_id,
                "seq_no": start_seq + index,
                "row_no": r.row_no,
                "sheet_name": r.sheet_name,
                "json_path": r.json_path[:512] if r.json_path else None,
                "record_label": r.record_label[:255],
                "payload": r.payload,
                "knowledge_text": r.knowledge_text,
                "anomalies": r.anomalies,
            }
            for index, r in enumerate(rows)
        ]
        await self.db.execute(insert(StructuredRecord), values)

    async def _mark_failed(
        self,
        row_id: uuid.UUID,
        message: str,
        *,
        read_count: int = 0,
        failed_count: int = 0,
    ) -> StructuredParse:
        """只改概览行的状态与原因；用 UPDATE 语句而不是 ORM 脏检查，
        因为调用方可能刚 rollback 过，实例属性已过期。

        失败时**不覆盖** record_count / skipped_count / fields：那些仍是上一份
        成功结果的事实。read_count / failed_count 描述的是本次失败尝试。
        """
        await self.db.execute(
            update(StructuredParse)
            .where(StructuredParse.id == row_id)
            .values(
                status=STRUCTURED_PARSE_FAILED,
                error_message=(message or "解析失败")[:_MAX_ERROR_CHARS],
                read_count=max(0, int(read_count)),
                failed_count=max(0, int(failed_count)),
            )
        )
        await self.db.commit()
        row = await self.db.get(StructuredParse, row_id)
        if row is None:  # 文件在解析期间被彻底删除
            raise NotFoundError("解析记录不存在")
        return row

    async def purge(self, doc_id: uuid.UUID) -> None:
        """彻底删除文件前清理解析结果（正常路径由 FK CASCADE 兜底，这里显式调用
        是为了在删除事务内就释放记录行，避免依赖数据库级联的执行时机）。"""
        await self.db.execute(delete(StructuredRecord).where(StructuredRecord.doc_id == doc_id))
        await self.db.execute(delete(StructuredParse).where(StructuredParse.doc_id == doc_id))
        await self.db.flush()
