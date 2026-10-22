"""真实中医知识数据导入中心（仅管理员）。

设计要点：
- 后端**直接读取** IMPORT_SOURCE_DIR（配置化）下的本机数据，前端不上传大文件；
- 扫描只读分析，返回数据集画像 + 语义化字段映射 + 真实样例，**不自动导入**；
- 管理员确认映射与范围后创建导入任务（pending → processing → completed / failed）；
- 落库与向量化全部复用既有链路（DocumentService / ParseService / IndexingService
  与资源表写入），本模块不重复实现。
"""

import os
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.application import dataset_scanner, import_service, upload_import_service
from src.application.audit_service import AuditService
from src.core.deps import get_client_ip, get_db, require_admin

router = APIRouter(prefix="/admin/import", tags=["admin-import"])


def _require_admin(request: Request) -> None:
    require_admin(request, "仅管理员可使用知识数据导入中心")


class ImportJobCreate(BaseModel):
    """创建导入任务（必须显式确认）。"""

    dataset_id: str
    target_type: str = Field(pattern="^(herb|prescription|theory|literature|document)$")
    kb_id: uuid.UUID | None = None
    limit: int | None = Field(default=None, ge=1, le=200)
    vectorize: bool = True
    # 二次确认闸门：前端需先展示映射预览并由用户勾选确认
    confirmed: bool = False


@router.get("/config")
async def import_config(request: Request) -> dict:
    """数据源配置状态（是否配置了 IMPORT_SOURCE_DIR）。"""
    _require_admin(request)
    from src.core.config import settings

    return {
        "source_dir": settings.IMPORT_SOURCE_DIR,
        "configured": bool(settings.IMPORT_SOURCE_DIR),
        "max_records_per_job": settings.IMPORT_MAX_RECORDS_PER_JOB,
    }


@router.post("/scan")
async def scan_datasets(
    request: Request,
    sample_rows: int = 5,
) -> dict:
    """扫描数据源目录（只读，不导入任何数据）。

    返回每个数据集的格式、大小、记录数（精确/估算）、字段结构、真实样例、
    自动识别的目标类型与字段映射结论。
    """
    _require_admin(request)
    result = dataset_scanner.scan_source_dir(limit=max(1, min(sample_rows, 20)))
    return result.model_dump()


@router.get("/datasets/{dataset_id}/preview")
async def preview_dataset(
    dataset_id: str,
    request: Request,
    sample_rows: int = 10,
) -> dict:
    """单数据集映射预览：字段映射结论 + 前若干条**真实**数据（不导入）。"""
    _require_admin(request)
    scan = dataset_scanner.scan_source_dir(limit=max(1, min(sample_rows, 20)))
    profile = next((d for d in scan.datasets if d.dataset_id == dataset_id), None)
    if profile is None:
        from src.core.exceptions import AppException

        raise AppException(404, f"数据集不存在：{dataset_id}")
    return {
        "dataset": profile.model_dump(),
        "source_dir": scan.source_dir,
        "hint": "确认映射无误后再创建导入任务；标记为「待确认」的字段不会写入系统。",
    }


class CleanupPayload(BaseModel):
    """测试数据清理请求（默认 dry run，真实删除需 confirm）。"""

    batch_ids: list[str] | None = None  # 指定回滚某些导入批次；为空表示清理手工/测试数据
    include_test_data: bool = True
    include_knowledge_bases: bool = False
    confirm: bool = False


@router.get("/cleanup/stats")
async def cleanup_stats(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """统计当前数据：知识库 / 中药 / 方剂 / 理论 / 文献 / documents / chunks / Milvus。"""
    _require_admin(request)
    from src.application import cleanup_service

    return await cleanup_service.collect_stats(db)


@router.post("/cleanup/plan")
async def cleanup_plan(
    payload: CleanupPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """清理计划（只读，不删任何数据）。"""
    _require_admin(request)
    from src.application import cleanup_service

    return await cleanup_service.plan_cleanup(
        db,
        batch_ids=payload.batch_ids,
        include_test_data=payload.include_test_data,
        include_knowledge_bases=payload.include_knowledge_bases,
    )


@router.post("/cleanup/execute")
async def cleanup_execute(
    payload: CleanupPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """执行清理（必须 confirm=True；用户/权限/配置/审计等系统数据不受影响）。"""
    _require_admin(request)
    from src.application import cleanup_service

    user = request.state.user
    result = await cleanup_service.execute_cleanup(
        db,
        batch_ids=payload.batch_ids,
        include_test_data=payload.include_test_data,
        include_knowledge_bases=payload.include_knowledge_bases,
        confirm=payload.confirm,
    )
    audit = AuditService(db)
    await audit.log(
        operator_id=user.id,
        operator_name=user.username,
        operation="cleanup_test_data",
        target_type="system",
        target_id="",
        detail={
            "batch_ids": payload.batch_ids,
            "include_test_data": payload.include_test_data,
            "include_knowledge_bases": payload.include_knowledge_bases,
            "deleted": result["deleted"],
            "error_count": result["error_count"],
        },
        ip=get_client_ip(request),
    )
    return result


@router.post("/jobs")
async def create_import_job(
    payload: ImportJobCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """创建并启动导入任务（需 confirmed=True）。"""
    _require_admin(request)
    user = request.state.user
    job = await import_service.create_job(
        db,
        dataset_id=payload.dataset_id,
        target_type=payload.target_type,
        kb_id=payload.kb_id,
        limit=payload.limit,
        user_id=user.id,
        vectorize=payload.vectorize,
        confirmed=payload.confirmed,
    )
    audit = AuditService(db)
    await audit.log(
        operator_id=user.id,
        operator_name=user.username,
        operation="import",
        target_type="import_job",
        target_id=str(job.id),
        detail={
            "dataset_id": job.dataset_id,
            "dataset_name": job.dataset_name,
            "target_type": job.target_type,
            "batch_id": job.batch_id,
            "limit": job.total,
            "kb_id": str(job.kb_id) if job.kb_id else None,
        },
        ip=get_client_ip(request),
    )
    import_service.start_job(job.id)
    return import_service.job_to_dict(job)


@router.get("/jobs")
async def list_import_jobs(
    request: Request,
    db: AsyncSession = Depends(get_db),
    limit: int = 20,
) -> dict:
    """导入任务列表（最新在前）。"""
    _require_admin(request)
    jobs = await import_service.list_jobs(db, limit=max(1, min(limit, 100)))
    return {"items": [import_service.job_to_dict(j) for j in jobs]}


@router.get("/jobs/latest")
async def latest_import_job(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """最近一次导入任务（前端轮询进度用）。"""
    _require_admin(request)
    job = await import_service.latest_job(db)
    return {"job": import_service.job_to_dict(job) if job else None}


@router.get("/jobs/{job_id}")
async def get_import_job(
    job_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """导入任务详情（含进度与失败明细）。"""
    _require_admin(request)
    job = await import_service.get_job(db, job_id)
    return import_service.job_to_dict(job)


@router.post("/jobs/{job_id}/cancel")
async def cancel_import_job(
    job_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """协作式取消进行中的导入任务（已导入数据保留，可用 batch_id 回滚）。"""
    _require_admin(request)
    job = await import_service.cancel_job(db, job_id)
    return import_service.job_to_dict(job)


# ── 资源批量挂载 + 向量化（复用 import_jobs 任务体系）──────────────────────


class ResourceVectorizeCreate(BaseModel):
    """创建资源批量挂载+向量化任务。"""

    resource_type: str = Field(pattern="^(herb|prescription|theory|literature)$")
    kb_id: uuid.UUID
    limit: int | None = Field(default=None, ge=1, le=5000)


@router.get("/resource-vectorize/stats")
async def resource_vectorize_stats(
    request: Request,
    kb_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """各资源类型：总数 / 已挂载 / 未挂载（用于选择批量范围）。"""
    _require_admin(request)
    from src.application import resource_batch_service

    return await resource_batch_service.stats(db, kb_id)


@router.post("/resource-vectorize")
async def create_resource_vectorize_job(
    payload: ResourceVectorizeCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """批量把资源挂载到知识库并向量化（后台任务，进度沿用 /jobs 轮询）。"""
    _require_admin(request)
    from src.application import resource_batch_service

    user = request.state.user
    job = await resource_batch_service.create_job(
        db,
        resource_type=payload.resource_type,
        kb_id=payload.kb_id,
        limit=payload.limit,
        user_id=user.id,
    )
    audit = AuditService(db)
    await audit.log(
        operator_id=user.id,
        operator_name=user.username,
        operation="import",
        target_type="resource_vectorize_job",
        target_id=str(job.id),
        detail={
            "resource_type": job.target_type,
            "kb_id": str(job.kb_id),
            "total": job.total,
            "batch_id": job.batch_id,
        },
        ip=get_client_ip(request),
    )
    snapshot = resource_batch_service.job_to_dict(job)
    resource_batch_service.start_job(job.id)
    return snapshot


@router.get("/resource-vectorize/latest")
async def latest_resource_vectorize_job(
    request: Request,
    resource_type: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """最近一次资源批量挂载任务（前端轮询进度）。"""
    _require_admin(request)
    from src.application import resource_batch_service

    job = await resource_batch_service.latest_job(db, resource_type)
    return {"job": resource_batch_service.job_to_dict(job) if job else None}


@router.post("/resource-vectorize/{job_id}/resume")
async def resume_resource_vectorize_job(
    job_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """断点续跑 / 只重试失败项：重新排队未完成与失败的条目（跳过已成功的）。"""
    _require_admin(request)
    from src.application import resource_batch_service

    job = await resource_batch_service.resume_job(
        db, job_id, getattr(request.state.user, "id", None)
    )
    snapshot = resource_batch_service.job_to_dict(job)
    resource_batch_service.start_job(job.id)
    return snapshot


# ── 上传文件导入（前端上传 → 自动识别 → 预览 → 确认导入）────────────────────


def _resolve_upload_path(dataset_id: str) -> str:
    """由 dataset_id 反查已上传文件路径（文件名固定为 <dataset_id><ext>）。"""
    import glob
    import os

    matches = glob.glob(os.path.join(upload_import_service.upload_root(), f"{dataset_id}.*"))
    if not matches:
        raise HTTPException(status_code=404, detail="上传文件不存在或已过期，请重新上传")
    return matches[0]


@router.post("/upload")
async def upload_import_file(
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """上传数据文件并自动分析（**只预览，不写业务数据**）。

    支持 CSV/TSV/XLSX/JSON/JSONL/TXT/MD/DOCX/PDF。
    自动判断数据类型（herb/prescription/theory/literature）并给出字段映射；
    无法可靠判断时返回 need_user_type=true，由用户手动选择（不猜测后直接导入）。
    """
    _require_admin(request)
    try:
        path, dataset_id = await upload_import_service.save_upload(file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        return await upload_import_service.build_preview(
            db, path=path, filename=file.filename or dataset_id, dataset_id=dataset_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 解析异常不暴露内部堆栈
        logger.warning(f"导入预览失败 dataset={dataset_id}: {exc}")
        raise HTTPException(status_code=400, detail=f"文件解析失败：{exc}") from exc


class UploadPreviewRequest(BaseModel):
    dataset_id: str
    target_type: str | None = None


@router.post("/upload/preview")
async def preview_upload(
    payload: UploadPreviewRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """按（可选的）指定类型重新生成预览与字段映射。"""
    _require_admin(request)
    path = _resolve_upload_path(payload.dataset_id)
    filename = os.path.basename(path)
    try:
        return await upload_import_service.build_preview(
            db,
            path=path,
            filename=filename,
            dataset_id=payload.dataset_id,
            target_type=payload.target_type,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class UploadCommitRequest(BaseModel):
    dataset_id: str
    target_type: str = Field(pattern="^(herb|prescription|theory|literature)$")
    # 允许显式传 null：表示"该字段不映射"（预览里把某列置空）
    field_mapping: dict[str, str | None] | None = None
    kb_id: uuid.UUID | None = None
    # 默认关闭：上传后不无条件启动大批量 BGE-M3 编码
    vectorize: bool = False
    # 二次确认闸门：必须先在预览中确认映射
    confirmed: bool = False


@router.post("/upload/commit")
async def commit_upload(
    payload: UploadCommitRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """确认后真正导入（pending → processing → completed / partial_success / failed）。

    规则：
    - 必须先预览并确认（confirmed=True）；
    - 自动识别失败时不允许导入（target_type 必须明确）；
    - 已存在同名资源只补充空字段，不覆盖用户已维护的真实数据；
    - 单条失败记录原因并继续，返回 inserted/updated/skipped/failed 统计。
    """
    _require_admin(request)
    user = request.state.user
    if not payload.confirmed:
        raise HTTPException(status_code=400, detail="请先确认字段映射与导入范围后再导入")

    path = _resolve_upload_path(payload.dataset_id)
    filename = os.path.basename(path)
    try:
        result = await upload_import_service.commit_import(
            db,
            path=path,
            filename=filename,
            dataset_id=payload.dataset_id,
            target_type=payload.target_type,
            user_id=user.id,
            mapping_override=payload.field_mapping,
            vectorize=payload.vectorize,
            kb_id=payload.kb_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    audit = AuditService(db)
    await audit.log(
        operator_id=user.id,
        operator_name=user.username,
        operation="upload_import",
        target_type="import_job",
        target_id=result["job_id"],
        detail={
            "dataset_id": payload.dataset_id,
            "target_type": payload.target_type,
            "batch_id": result["batch_id"],
            "inserted": result["inserted"],
            "updated": result["updated"],
            "skipped": result["skipped"],
            "failed": result["failed"],
        },
        ip=get_client_ip(request),
    )
    return result


@router.get("/upload/jobs/{dataset_id}")
async def upload_job_status(
    dataset_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """按 dataset_id 查询最近一次上传导入任务（状态 / 统计 / 失败明细）。"""
    _require_admin(request)
    from src.domain.models import ImportJob

    from sqlalchemy import select as sa_select

    job = (
        await db.scalars(
            sa_select(ImportJob)
            .where(ImportJob.dataset_id == dataset_id)
            .order_by(ImportJob.created_at.desc())
            .limit(1)
        )
    ).first()
    return {"job": upload_import_service.job_to_dict(job)}
