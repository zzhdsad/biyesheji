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
        """记录一条审计日志（非阻塞：审计写入失败不影响业务提交）。

        BUG-012：资源类 CRUD 只有 flush，log() 是唯一提交点。旧实现把审计写入
        和业务提交放在同一个 try 里，审计失败时 rollback 会连带回滚已 flush
        的业务变更（数据静默丢失，且调用方仍按成功返回）。现改为：
        1) 审计写入包在 SAVEPOINT 内——审计失败只回滚该 savepoint；
        2) 随后照常提交业务——DB 健康时业务与审计在同一事务内原子落盘；
        3) 业务提交失败必须抛出（调用方转为错误响应），不允许静默丢失。

        Args:
            operator_id: 操作人 ID（匿名操作为 None）
            operator_name: 操作人姓名/用户名（冗余，用户删除后仍可追溯）
            operation: 操作类型（create/update/delete/disable/restore/login 等）
            target_type: 目标对象类型（user/kb/document/message/config）
            target_id: 目标对象 ID
            detail: 变更详情
            ip: 操作来源 IP
        """
        entry = AuditLog(
            operator_id=operator_id,
            operator_name=operator_name,
            operation=operation,
            target_type=target_type,
            target_id=str(target_id),
            detail=detail or {},
            ip=ip,
        )
        # 1) 审计写入：SAVEPOINT 隔离，失败不影响同事务内的业务变更
        try:
            async with self.db.begin_nested():
                self.db.add(entry)
        except Exception as exc:
            # 审计缺失是合规事件（error 级），但不得回滚业务、不得中断请求
            logger.error(
                f"审计日志写入失败（业务已提交，审计缺失需人工补录）: "
                f"operation={operation} target={target_type}:{target_id} 原因={exc}"
            )
        # 2) 业务提交：唯一提交点，失败必须向上抛出（不允许静默丢失）
        await self.db.commit()

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
