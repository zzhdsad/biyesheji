"""审计日志路由（BUSINESS_RULES §7）。

仅 admin 可访问，支持按操作类型、目标类型、操作人、时间范围筛选。
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.audit_service import AuditService
from src.infrastructure.database import get_db

router = APIRouter(prefix="/audit", tags=["audit"])


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    operator_id: uuid.UUID | None
    operator_name: str
    operation: str
    target_type: str
    target_id: str
    detail: dict
    ip: str
    created_at: datetime


def _require_admin(request: Request) -> None:
    user = request.state.user
    if user.role != "admin":
        from src.core.exceptions import PermissionDeniedError
        raise PermissionDeniedError("仅管理员可查看审计日志")


@router.get("", response_model=list[AuditLogOut])
async def list_audit_logs(
    request: Request,
    operation: str | None = Query(default=None),
    target_type: str | None = Query(default=None),
    operator_id: uuid.UUID | None = Query(default=None),
    start_time: datetime | None = Query(default=None),
    end_time: datetime | None = Query(default=None),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list:
    """查询审计日志（仅 admin，支持多条件筛选 + 分页）。"""
    _require_admin(request)
    svc = AuditService(db)
    return await svc.list_logs(
        operation=operation,
        target_type=target_type,
        operator_id=operator_id,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        offset=offset,
    )
