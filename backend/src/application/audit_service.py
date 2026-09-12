"""审计日志服务：记录关键数据变更操作 + 查询审计记录。

BUSINESS_RULES §7 审计与日志：
- 记录操作：用户管理、知识库增删改、文档增删改、问答记录、系统配置修改
- 审计字段：操作人、操作类型、目标对象、详情、IP、时间
- 问答审计：管理员可查看所有问答记录，按状态筛选
"""

import uuid
from datetime import datetime

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models import AuditLog


class AuditService:
    """审计日志写入与查询。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def log(
        self,
        operator_id: uuid.UUID | None,
        operator_name: str,
        operation: str,
        target_type: str,
        target_id: str = "",
        detail: dict | None = None,
        ip: str = "",
    ) -> None:
        """记录一条审计日志（非阻塞：失败仅记日志，不影响主业务）。

        Args:
            operator_id: 操作人 ID（匿名操作为 None）
            operator_name: 操作人姓名/用户名（冗余，用户删除后仍可追溯）
            operation: 操作类型（create/update/delete/disable/restore/login 等）
            target_type: 目标对象类型（user/kb/document/message/config）
            target_id: 目标对象 ID
            detail: 变更详情
            ip: 操作来源 IP
        """
        try:
            entry = AuditLog(
                operator_id=operator_id,
                operator_name=operator_name,
                operation=operation,
                target_type=target_type,
                target_id=str(target_id),
                detail=detail or {},
                ip=ip,
            )
            self.db.add(entry)
            await self.db.commit()
        except Exception as exc:
            logger.warning(f"审计日志写入失败（不影响主业务）: {exc}")
            await self.db.rollback()

    async def list_logs(
        self,
        operation: str | None = None,
        target_type: str | None = None,
        operator_id: uuid.UUID | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditLog]:
        """查询审计日志（管理员用，支持多条件筛选 + 分页）。"""
        stmt = select(AuditLog)
        if operation:
            stmt = stmt.where(AuditLog.operation == operation)
        if target_type:
            stmt = stmt.where(AuditLog.target_type == target_type)
        if operator_id:
            stmt = stmt.where(AuditLog.operator_id == operator_id)
        if start_time:
            stmt = stmt.where(AuditLog.created_at >= start_time)
        if end_time:
            stmt = stmt.where(AuditLog.created_at <= end_time)
        stmt = stmt.order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)
        rows = (await db_scalars(self.db, stmt)).all()
        return list(rows)


async def db_scalars(db: AsyncSession, stmt):
    """封装 db.scalars 便于一致调用。"""
    return await db.scalars(stmt)
