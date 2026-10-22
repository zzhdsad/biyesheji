"""管理中心统一回收站路由装配（中药 / 方剂 / 中医理论 / 文献 / 分类 / 标签）。

为什么做成"装配函数"而不是每个模块复制一遍：
- 之前 knowledge_bases / documents / users 三处回收站是复制粘贴实现，口径已经
  出现不一致（保留天数写死 7 天等）；再让 6 个资源模块各写一份会继续放大差异。
- 这里把**生命周期**收敛到 src/application/trash_service.py，本模块只负责
  HTTP 层（权限校验 → 调用 → 审计 → 响应），各资源模块一行装配即可。

路由路径说明：
- 资源模块已有 ``GET /{id}`` 详情路由，为避免 ``/trash`` 被当成 id 解析，
  回收站列表统一用 ``GET /trash/list``（与 documents 现有口径一致）。

安全：
- 全部端点仅 admin（后端二次校验，不依赖前端隐藏按钮）；
- 彻底删除只处理**已在回收站**的记录，必须先软删除；
- 单条失败只记录原因并继续（trash_service 内 savepoint 隔离）。
"""

from __future__ import annotations

import uuid
from typing import Any, Callable

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application import trash_service
from src.application.audit_service import AuditService
from src.core.deps import get_client_ip
from src.core.exceptions import AppException
from src.infrastructure.database import get_db


class IdsRequest(BaseModel):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=2000)


def _require_admin(request: Request):
    user = request.state.user
    if getattr(user, "role", None) != "admin":
        raise AppException(403, "仅管理员可执行该操作")
    return user


def _trash_payload(
    rows: list[Any], total: int, serialize: Callable[[Any], Any], limit: int | None, offset: int
) -> dict:
    return {
        "items": [serialize(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def register_resource_trash_routes(
    router: APIRouter,
    *,
    model: type,
    label: str,
    serialize: Callable[[Any], Any],
    resource_type: str | None = None,
    audit_target_type: str | None = None,
) -> None:
    """给资源路由挂载回收站生命周期端点。

    Args:
        model: ORM 模型（需具备 deleted_at 列）
        label: 中文名，用于提示文案（如"中药"）
        serialize: 对象 → 响应字典（复用各模块已有的 *_to_out）
        resource_type: 可挂载进 KB 的资源类型（herb/prescription/theory/literature）；
            分类/标签传 None（无向量与 KB 挂载需要清理）
        audit_target_type: 审计 target_type，默认取 resource_type 或 model 表名
    """
    target_type = audit_target_type or resource_type or model.__tablename__

    @router.post("/batch-delete")
    async def batch_delete(
        payload: IdsRequest,
        request: Request,
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        """批量删除 → 移入回收站（软删除，仅 admin）。"""
        user = _require_admin(request)
        impact = await trash_service.trash_impact(db, model, payload.ids)
        success = await trash_service.soft_delete_many(db, model, payload.ids)
        await db.commit()
        audit = AuditService(db)
        await audit.log(
            operator_id=user.id,
            operator_name=user.username,
            operation="batch_delete",
            target_type=target_type,
            target_id=",".join(str(i) for i in payload.ids[:20]),
            detail={"count": success, "requested": len(payload.ids), "kb_mounts": impact["kb_mounts"]},
            ip=get_client_ip(request),
        )
        days = trash_service.retention_days()
        return {
            "total": len(payload.ids),
            "success": success,
            "message": f"已移入回收站 {success} 条，{days} 天内可恢复",
        }

    @router.get("/trash/list")
    async def list_trash(
        request: Request,
        limit: int | None = Query(default=None, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        """回收站列表（仅 admin），进入时自动清理超过保留期的条目。"""
        _require_admin(request)
        rows, total = await trash_service.list_trash(
            db, model, limit=limit, offset=offset, resource_type=resource_type
        )
        return _trash_payload(rows, total, serialize, limit, offset)

    @router.post("/batch-restore")
    async def batch_restore(
        payload: IdsRequest,
        request: Request,
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        """批量恢复（仅 admin）。"""
        user = _require_admin(request)
        success = await trash_service.restore_many(db, model, payload.ids)
        await db.commit()
        audit = AuditService(db)
        await audit.log(
            operator_id=user.id,
            operator_name=user.username,
            operation="batch_restore",
            target_type=target_type,
            target_id=",".join(str(i) for i in payload.ids[:20]),
            detail={"count": success},
            ip=get_client_ip(request),
        )
        return {"total": len(payload.ids), "success": success,
                "message": f"已恢复 {success} 条"}

    @router.post("/{item_id}/restore")
    async def restore_one(
        item_id: uuid.UUID,
        request: Request,
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        """单条恢复（仅 admin）。"""
        user = _require_admin(request)
        success = await trash_service.restore_many(db, model, [item_id])
        if not success:
            raise AppException(404, f"回收站中不存在该{label}")
        await db.commit()
        audit = AuditService(db)
        await audit.log(
            operator_id=user.id,
            operator_name=user.username,
            operation="restore",
            target_type=target_type,
            target_id=str(item_id),
            ip=get_client_ip(request),
        )
        return {"id": str(item_id), "restored": True}

    @router.delete("/{item_id}/purge")
    async def purge_one(
        item_id: uuid.UUID,
        request: Request,
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        """单条彻底删除（仅 admin，不可恢复；只处理回收站中的记录）。"""
        user = _require_admin(request)
        result = await trash_service.purge_ids(
            db, model, [item_id], resource_type=resource_type
        )
        if not result["purged"] and result["failed"]:
            raise AppException(422, f"彻底删除失败：{result['failed'][0]['reason']}")
        if not result["purged"]:
            raise AppException(404, f"回收站中不存在该{label}")
        audit = AuditService(db)
        await audit.log(
            operator_id=user.id,
            operator_name=user.username,
            operation="purge",
            target_type=target_type,
            target_id=str(item_id),
            ip=get_client_ip(request),
        )
        return {"id": str(item_id), "purged": True}

    @router.post("/batch-purge")
    async def batch_purge(
        payload: IdsRequest,
        request: Request,
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        """批量彻底删除（仅 admin）；单条失败记录原因并继续其余记录。"""
        user = _require_admin(request)
        result = await trash_service.purge_ids(
            db, model, payload.ids, resource_type=resource_type
        )
        audit = AuditService(db)
        await audit.log(
            operator_id=user.id,
            operator_name=user.username,
            operation="batch_purge",
            target_type=target_type,
            target_id=",".join(str(i) for i in payload.ids[:20]),
            detail={"purged": result["total"], "failed": len(result["failed"])},
            ip=get_client_ip(request),
        )
        return {
            "total": len(payload.ids),
            "purged": result["total"],
            "failed": result["failed"],
        }

    @router.delete("/trash/purge-all")
    async def purge_all_trash(
        request: Request,
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        """一键清空回收站（仅 admin）：彻底删除当前回收站中的全部条目。"""
        user = _require_admin(request)
        before = await db.scalar(
            select(model).where(model.deleted_at.is_not(None)).limit(1)
        )
        result = await trash_service.purge_all(db, model, resource_type=resource_type)
        audit = AuditService(db)
        await audit.log(
            operator_id=user.id,
            operator_name=user.username,
            operation="purge_all_trash",
            target_type=target_type,
            target_id="",
            detail={"purged": result["total"], "failed": len(result["failed"])},
            ip=get_client_ip(request),
        )
        return {
            "purged": result["total"],
            "failed": result["failed"],
            "empty": before is None,
            "message": f"已彻底删除 {result['total']} 条，失败 {len(result['failed'])} 条",
        }
