"""通用知识导入 API：普通导入 / 关联导入 / 索引一致性。

路由只做鉴权、参数校验与编排，业务逻辑在各 application 服务：
- 文件类型与编码识别：file_type_service
- 结构化解析：tabular_parser
- Schema 分析：schema_analyzer
- 主外键发现：relation_discovery
- 实体与关系持久化：entity_import_service
- 索引一致性：index_state_service

文档类文件（PDF/DOCX/TXT/MD）**一律复用既有 DocumentService 上传链路**，
保证既有 RAG 行为不变；结构化文件按记录入库，不拼成整段文本（决策 6）。

权限：全部仅管理员（AGENTS.md：导入能力仅 admin），
且涉及知识库的写操作在服务端用 require_kb_write 二次校验。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from io import BytesIO
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile as StarletteUploadFile

from src.application import file_type_service as fts
from src.application import schema_analyzer, tabular_parser
from src.application.entity_import_service import EntityImportService
from src.application.index_state_service import IndexStateService
from src.application.mapping_template_service import (
    MappingTemplateService,
    TableMapping,
    TemplateConfig,
    TemplateError,
    dataset_fingerprint,
)
from src.application.relation_discovery import (
    ForeignKeyCandidate,
    JunctionCandidate,
    discover_relations,
)
from src.core.config import settings
from src.core.deps import get_db, require_admin, require_kb_read, require_kb_write
from src.domain.models import (
    KG_STATUS_CONFIRMED,
    KG_STATUS_PENDING,
    KG_STATUS_REJECTED,
    GenericEntity,
    GenericRelation,
    ImportFile,
    ImportJob,
    ImportMappingTemplate,
    ImportRecord,
    User,
)

router = APIRouter(prefix="/import", tags=["general-import"])

# 单次上传的文件数上限（防止一次拖入整个磁盘）
MAX_FILES = 500
# 预览行数
PREVIEW_ROWS = 5
# 扫描时用于 Schema/主键候选分析的采样行数
SCAN_SAMPLE = 500
# 暂存目录（与上传共用 uploads 卷）
STAGING_SUBDIR = "general_import_sessions"
# Schema 分析采样行数
ANALYZE_SAMPLE = 2000


def _admin(request: Request) -> User:
    require_admin(request, "仅管理员可使用通用知识导入")
    return request.state.user


def _staging_root() -> str:
    base = os.getenv("UPLOAD_DIR") or os.path.join(os.getcwd(), "uploads")
    path = os.path.join(base, STAGING_SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path


def _safe_rel(rel: str) -> str:
    """规范化相对路径，阻断 ../ 与绝对路径（防目录穿越）。"""
    text = (rel or "").replace("\\", "/").strip()
    parts = [p for p in text.split("/") if p not in ("", ".", "..")]
    if not parts:
        raise ValueError(f"非法相对路径：{rel!r}")
    return "/".join(parts)


def _index_environment() -> dict[str, Any]:
    """当前索引环境事实（供前端展示与删除前的环境门禁）。

    只有集合名带 ``_dev`` / ``_test`` 后缀、且 ENV 不是 prod 时，才认为删除
    Milvus 向量是允许的：修复接口能删向量，绝不能因为前端传了 kb_id 就跨到
    正式集合上操作。
    """
    collection = (settings.MILVUS_COLLECTION or "").strip()
    env = (settings.ENV or "").strip().lower()
    isolated = collection.endswith("_dev") or collection.endswith("_test")
    try:
        database = make_url(settings.DATABASE_URL).database or ""
    except Exception:  # noqa: BLE001 - 仅用于展示，解析失败不应阻断接口
        database = ""
    return {
        "env": env,
        "database": database,
        "milvus_collection": collection,
        "isolated_collection": isolated,
        "destructive_allowed": isolated and env != "prod",
    }


def _ensure_expected_collection(expected: str | None) -> None:
    """客户端声明的目标集合必须与当前后端一致（409，防跨环境误操作）。"""
    if not expected:
        return
    actual = (settings.MILVUS_COLLECTION or "").strip()
    if expected.strip() != actual:
        raise HTTPException(
            status_code=409,
            detail=(
                f"目标 Milvus 集合与环境不一致：前端为 {expected!r}，"
                f"当前后端为 {actual!r}，请刷新环境信息后重试"
            ),
        )


def _ensure_destructive_allowed() -> None:
    """删除向量前校验环境：正式集合/生产环境一律拒绝。"""
    facts = _index_environment()
    if not facts["destructive_allowed"]:
        raise HTTPException(
            status_code=403,
            detail=(
                "当前环境不允许删除向量："
                f"ENV={facts['env']}，集合={facts['milvus_collection']}。"
                "删除仅允许在隔离环境（*_dev / *_test 集合且非生产）执行"
            ),
        )


async def _ensure_kb_read(db: AsyncSession, request: Request, kb_id: uuid.UUID | None) -> None:
    """指定知识库时二次校验读权限（admin 已隐含全库权限，此处保留显式边界）。"""
    if kb_id is None:
        return
    await require_kb_read(db, _admin(request), kb_id)


class FilePlan(BaseModel):
    """提交阶段指定的单文件导入方式。"""

    path: str
    entity_type: str
    pk_field: str | None = None


class RelationPlanItem(BaseModel):
    child_table: str
    child_field: str
    parent_table: str
    parent_field: str
    relation_type: str = "related_to"
    confidence: float = 1.0


class JunctionPlanItem(BaseModel):
    table: str
    left_table: str
    left_field: str
    right_table: str
    right_field: str
    relation_type: str = "related_to"
    confidence: float = 1.0


class GeneralCommitRequest(BaseModel):
    session_id: str
    kb_id: uuid.UUID
    files: list[FilePlan]
    import_documents: bool = True


class RelationalCommitRequest(BaseModel):
    session_id: str
    kb_id: uuid.UUID
    primary_keys: dict[str, str] = Field(default_factory=dict)
    foreign_keys: list[RelationPlanItem] = Field(default_factory=list)
    junctions: list[JunctionPlanItem] = Field(default_factory=list)
    auto_confirm: bool = False
    # 复用既有映射模板：给出后，未显式指定的主键/关系由模板补齐
    template_id: uuid.UUID | None = None
    save_as_template: str | None = None


class RelationStatusRequest(BaseModel):
    kb_id: uuid.UUID
    relation_ids: list[uuid.UUID]
    status: str = Field(pattern="^(pending|confirmed|rejected)$")


class IndexApplyRequest(BaseModel):
    kb_id: uuid.UUID | None = None
    fix_flags: bool = True
    delete_orphans: bool = False
    delete_duplicates: bool = False
    confirmed: bool = False
    # 前端展示/操作时绑定的集合：与后端不一致时拒绝，防止跨环境误操作
    expected_milvus_collection: str | None = None


class IndexRebuildRequest(BaseModel):
    kb_id: uuid.UUID | None = None
    retry_failed: bool = False
    expected_milvus_collection: str | None = None


# ── 上传与识别 ──────────────────────────────────────────────────────────────


@router.post("/scan")
async def scan_files(
    request: Request,
    files: list[UploadFile] = File(...),
    paths: str | None = Form(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """多文件 / 文件夹递归上传后的**只读识别**：类型、编码、记录数、预览、错误。

    - paths：与 files 顺序一致的相对路径数组（JSON 字符串），用于文件夹递归时
      保留目录结构；未提供时用文件名兜底。
    - 本接口**不写入任何业务数据**，只把文件暂存到会话目录供 commit 阶段使用。
    """
    _admin(request)
    if not files:
        return {"session_id": "", "files": [], "errors": ["未选择任何文件"]}
    if len(files) > MAX_FILES:
        return {"session_id": "", "files": [], "errors": [f"一次最多上传 {MAX_FILES} 个文件"]}

    import json as _json

    rel_paths: list[str] = []
    if paths:
        try:
            raw = _json.loads(paths)
            if isinstance(raw, list):
                rel_paths = [str(x) for x in raw]
        except Exception:  # noqa: BLE001 - 前端未传/格式错误时按文件名兜底
            rel_paths = []

    session_id = uuid.uuid4().hex
    session_dir = os.path.join(_staging_root(), session_id)
    os.makedirs(session_dir, exist_ok=True)

    scanned: list[dict[str, Any]] = []
    for index, upload in enumerate(files):
        rel = rel_paths[index] if index < len(rel_paths) else (upload.filename or f"file-{index}")
        try:
            rel = _safe_rel(rel)
        except ValueError as exc:
            scanned.append({"path": rel, "error": str(exc), "supported": False})
            continue

        data = await upload.read()
        if not data:
            scanned.append({"path": rel, "error": "文件为空", "supported": False})
            continue

        target = os.path.join(session_dir, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(data)

        fmt = fts.detect_format(os.path.basename(rel), data[:64])
        encoding = ""
        row_count = 0
        preview: list[dict[str, Any]] = []
        fields: list[dict[str, Any]] = []
        pk_candidates: list[dict[str, Any]] = []
        error: str | None = None

        if not fts.is_supported(fmt):
            error = fts.describe_unsupported(os.path.basename(rel), fmt)
        elif fts.is_structured(fmt):
            encoding = fts.detect_encoding(data)
            try:
                sample_rows = list(
                    tabular_parser.iter_rows(target, fmt, encoding, max_rows=SCAN_SAMPLE)
                )
                preview = [
                    {"row_no": row.row_no, "data": row.data} for row in sample_rows[:PREVIEW_ROWS]
                ]
                # 一并给出主键候选：否则普通导入页面只能让用户手填主键，
                # 不填就静默生成 0 个实体（端到端验收时实测踩到）。
                profile = schema_analyzer.analyze_rows(
                    sample_rows,
                    table_name=os.path.splitext(os.path.basename(rel))[0],
                    file_name=os.path.basename(rel),
                    sample_size=SCAN_SAMPLE,
                )
                fields = [
                    {
                        "name": f.name,
                        "type": f.inferred_type,
                        "is_unique": f.is_unique,
                        "null_ratio": round(f.null_ratio, 3),
                    }
                    for f in profile.fields
                ]
                pk_candidates = [
                    {
                        "fields": list(c.fields),
                        "uniqueness": round(c.uniqueness, 3),
                        "confidence": c.confidence,
                        "reason": c.reason,
                    }
                    for c in profile.primary_key_candidates
                ]
                row_count, count_error = tabular_parser.count_rows(target, fmt, encoding)
                if count_error:
                    error = count_error
            except tabular_parser.TabularParseError as exc:
                error = exc.message
        else:
            row_count = 0

        scanned.append(
            {
                "path": rel,
                "file_name": os.path.basename(rel),
                "size_bytes": len(data),
                "detected_format": fmt,
                "encoding": encoding,
                "row_count": row_count,
                "preview": preview,
                "fields": fields,
                "primary_key_candidates": pk_candidates,
                "error": error,
                "supported": bool(fmt) and fts.is_supported(fmt),
            }
        )

    return {
        "session_id": session_id,
        "files": scanned,
        "errors": [f["error"] for f in scanned if f.get("error")],
    }


def _session_dir(session_id: str) -> str:
    return os.path.join(_staging_root(), _safe_rel(session_id))


# ── 普通导入提交 ────────────────────────────────────────────────────────────


@router.post("/general/commit")
async def commit_general(
    request: Request,
    payload: GeneralCommitRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """普通导入落库：文档走既有 RAG 链路，结构化文件按记录入库。"""
    user = _admin(request)
    session_dir = _session_dir(payload.session_id)
    if not os.path.isdir(session_dir):
        return {"error": "会话不存在或已过期，请重新上传文件"}

    svc = EntityImportService(db)
    await svc.require_write(user, payload.kb_id)
    commit_errors: list[dict[str, Any]] = []

    job = ImportJob(
        batch_id=f"gen-{uuid.uuid4().hex[:12]}",
        dataset_id=payload.session_id,
        dataset_name="通用导入（普通）",
        target_type="document",
        import_mode="normal",
        kb_id=payload.kb_id,
        status="processing",
        created_by=user.id,
    )
    db.add(job)
    await db.flush()

    documents_imported = 0
    records = 0
    entities = 0
    errors: list[dict[str, Any]] = []

    for plan in payload.files:
        file_path = os.path.join(session_dir, _safe_rel(plan.path))
        if not os.path.isfile(file_path):
            errors.append({"path": plan.path, "message": "文件不存在"})
            continue
        data = open(file_path, "rb").read()
        name = os.path.basename(plan.path)
        fmt = fts.detect_format(name, data[:64])

        import_file = ImportFile(
            job_id=job.id,
            relative_path=_safe_rel(plan.path),
            file_name=name,
            file_ext=os.path.splitext(name)[1].lower(),
            size_bytes=len(data),
            file_hash=hashlib.sha256(data).hexdigest(),
            encoding=fts.detect_encoding(data) if fts.is_structured(fmt) else "",
            detected_format=fmt,
            status="parsed",
        )
        db.add(import_file)
        await db.flush()

        if fts.is_document(fmt):
            if not payload.import_documents:
                continue
            try:
                from src.application.document_service import DocumentService

                upload = StarletteUploadFile(file=BytesIO(data), filename=name)
                await DocumentService(db).upload(payload.kb_id, upload, user=user)
                documents_imported += 1
            except Exception as exc:  # noqa: BLE001 - 单文件失败不影响其他文件
                errors.append({"path": plan.path, "message": f"文档入库失败：{exc}"})
                import_file.status = "failed"
                import_file.error_message = str(exc)[:2000]
            continue

        # 结构化文件：按记录入库，保留原始 Schema（不拼文本块）
        encoding = fts.detect_encoding(data)
        row_errors: list[dict[str, Any]] = []
        try:
            rows = list(
                tabular_parser.iter_rows(
                    file_path,
                    fmt,
                    encoding,
                    on_error=lambda row_no, msg: row_errors.append(
                        {"path": plan.path, "row_no": row_no, "message": msg}
                    ),
                )
            )
        except tabular_parser.TabularParseError as exc:
            errors.append({"path": plan.path, "message": exc.message})
            import_file.status = "failed"
            import_file.error_message = exc.message
            continue
        # 单行脏数据只跳过该行，其余照常入库，错误随结果返回给前端展示
        errors.extend(row_errors)

        import_file.row_count = len(rows)
        written, rec_errors = await svc.save_records(
            job_id=job.id,
            file_id=import_file.id,
            kb_id=payload.kb_id,
            file_hash=import_file.file_hash,
            entity_type=plan.entity_type,
            rows=rows,
            file_name=name,
        )
        records += written
        errors.extend({"path": plan.path, **e.__dict__} for e in rec_errors)

        if plan.pk_field:
            index, ent_errors = await svc.upsert_entities(
                kb_id=payload.kb_id,
                entity_type=plan.entity_type,
                rows=rows,
                pk_field=plan.pk_field,
                file_id=import_file.id,
                file_hash=import_file.file_hash,
                batch_id=job.batch_id,
                created_by=user.id,
                file_name=name,
            )
            entities += len(index)
            errors.extend({"path": plan.path, **e.__dict__} for e in ent_errors)

    job.processed = len(payload.files)
    job.succeeded = len(payload.files) - len({e["path"] for e in errors})
    job.failed = len(errors)
    job.status = "completed"
    await db.commit()
    shutil.rmtree(session_dir, ignore_errors=True)

    return {
        "job_id": str(job.id),
        "documents": documents_imported,
        "records": records,
        "entities": entities,
        "failed": len(errors),
        "errors": errors[:50],
    }


# ── 关联导入分析与提交 ──────────────────────────────────────────────────────


@router.post("/relational/analyze")
async def analyze_relational(
    request: Request,
    files: list[UploadFile] = File(...),
    paths: str | None = Form(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """关联导入分析：Schema + 主键候选 + 外键候选 + 关联表候选（只读）。"""
    _admin(request)
    if not files:
        return {"session_id": "", "tables": [], "errors": ["未选择任何文件"]}

    import json as _json

    rel_paths: list[str] = []
    if paths:
        try:
            raw = _json.loads(paths)
            if isinstance(raw, list):
                rel_paths = [str(x) for x in raw]
        except Exception:  # noqa: BLE001
            rel_paths = []

    session_id = uuid.uuid4().hex
    session_dir = os.path.join(_staging_root(), session_id)
    os.makedirs(session_dir, exist_ok=True)

    tables: list[dict[str, Any]] = []
    rows_by_table: dict[str, list] = {}
    errors: list[str] = []

    for index, upload in enumerate(files):
        rel = rel_paths[index] if index < len(rel_paths) else (upload.filename or f"file-{index}")
        try:
            rel = _safe_rel(rel)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        data = await upload.read()
        target = os.path.join(session_dir, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(data)

        name = os.path.basename(rel)
        fmt = fts.detect_format(name, data[:64])
        table_name = os.path.splitext(name)[0] or f"table-{index}"
        if not fts.is_structured(fmt):
            errors.append(f"{name}：{fts.describe_unsupported(name, fmt)}")
            continue
        encoding = fts.detect_encoding(data)
        try:
            parsed = list(tabular_parser.iter_rows(target, fmt, encoding))
        except tabular_parser.TabularParseError as exc:
            errors.append(f"{name}：{exc.message}")
            continue

        rows_by_table[table_name] = parsed
        profile = schema_analyzer.analyze_rows(
            parsed, table_name=table_name, file_name=name, sample_size=ANALYZE_SAMPLE
        )
        tables.append(
            {
                "table": table_name,
                "file": name,
                "row_count": profile.row_count,
                "analyzed_rows": profile.analyzed_rows,
                "sampled": profile.sampled,
                "fields": [
                    {
                        "name": f.name,
                        "type": f.inferred_type,
                        "null_ratio": round(f.null_ratio, 3),
                        "unique_ratio": round(f.unique_ratio, 3),
                        "is_unique": f.is_unique,
                    }
                    for f in profile.fields
                ],
                "primary_key_candidates": [
                    {
                        "fields": list(c.fields),
                        "uniqueness": round(c.uniqueness, 3),
                        "confidence": c.confidence,
                        "reason": c.reason,
                    }
                    for c in profile.primary_key_candidates
                ],
                "preview": [{"row_no": r.row_no, "data": r.data} for r in parsed[:PREVIEW_ROWS]],
            }
        )

    if not rows_by_table:
        return {"session_id": session_id, "tables": tables, "errors": errors or ["没有可分析的结构化文件"]}

    profiles = [
        schema_analyzer.analyze_rows(rows, table_name=name, sample_size=ANALYZE_SAMPLE)
        for name, rows in rows_by_table.items()
    ]
    plan = discover_relations(profiles)

    return {
        "session_id": session_id,
        "tables": tables,
        "primary_keys": {t: list(v) for t, v in plan.primary_keys.items()},
        "foreign_keys": [
            {
                "child_table": fk.child_table,
                "child_field": fk.child_field,
                "parent_table": fk.parent_table,
                "parent_field": fk.parent_field,
                "cardinality": fk.cardinality,
                "confidence": fk.confidence,
                "coverage": fk.coverage,
                "evidence": fk.evidence,
                "status": fk.status,
            }
            for fk in plan.foreign_keys
        ],
        "junctions": [
            {
                "table": j.table,
                "left_table": j.left_table,
                "left_field": j.left_field,
                "right_table": j.right_table,
                "right_field": j.right_field,
                "label": j.label,
                "confidence": j.confidence,
                "evidence": j.evidence,
                "relation_type": j.relation_type,
                "status": j.status,
            }
            for j in plan.junctions
        ],
        # 数据集指纹：前端据此查询可复用的映射模板
        "dataset_fingerprint": dataset_fingerprint(
            {p.table_name: [f.name for f in p.fields] for p in profiles}
        ),
        "errors": errors,
    }


@router.post("/relational/commit")
async def commit_relational(
    request: Request,
    payload: RelationalCommitRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """关联导入落库：实体 + 关系 +（确认后）KG。"""
    user = _admin(request)
    session_dir = _session_dir(payload.session_id)
    if not os.path.isdir(session_dir):
        return {"error": "会话不存在或已过期，请重新上传文件"}

    # 从暂存目录重新解析（服务端以文件为准，避免前端篡改分析结果）
    rows_by_table: dict[str, list] = {}
    files_by_table: dict[str, Any] = {}
    # 解析级错误（坏行被跳过、整文件解析失败），需与入库错误一起返回给前端
    commit_errors: list[dict[str, Any]] = []
    job = ImportJob(
        batch_id=f"rel-{uuid.uuid4().hex[:12]}",
        dataset_id=payload.session_id,
        dataset_name="通用导入（关联）",
        target_type="generic",
        import_mode="relational",
        kb_id=payload.kb_id,
        status="processing",
        created_by=user.id,
    )
    db.add(job)
    await db.flush()

    for root, _dirs, names in os.walk(session_dir):
        for name in names:
            abs_path = os.path.join(root, name)
            rel = os.path.relpath(abs_path, session_dir).replace("\\", "/")
            data = open(abs_path, "rb").read()
            fmt = fts.detect_format(name, data[:64])
            if not fts.is_structured(fmt):
                continue
            table_name = os.path.splitext(name)[0]
            encoding = fts.detect_encoding(data)
            try:
                rows_by_table[table_name] = list(
                    tabular_parser.iter_rows(
                        abs_path,
                        fmt,
                        encoding,
                        on_error=lambda row_no, msg, _n=name: commit_errors.append(
                            {"file_name": _n, "row_no": row_no, "message": msg}
                        ),
                    )
                )
            except tabular_parser.TabularParseError as exc:
                logger.warning(f"[general_import] {rel} 解析失败: {exc.message}")
                commit_errors.append({"file_name": name, "row_no": 0, "message": exc.message})
                continue

            import_file = ImportFile(
                job_id=job.id,
                relative_path=rel,
                file_name=name,
                file_ext=os.path.splitext(name)[1].lower(),
                size_bytes=len(data),
                file_hash=hashlib.sha256(data).hexdigest(),
                encoding=encoding,
                detected_format=fmt,
                row_count=len(rows_by_table[table_name]),
                status="parsed",
            )
            db.add(import_file)
            await db.flush()
            files_by_table[table_name] = import_file

    if not rows_by_table:
        job.status = "failed"
        job.error_message = "没有可用于导入的结构化文件"
        job.failed = len(commit_errors)
        await db.commit()
        return {"error": job.error_message, "errors": commit_errors[:50]}

    svc = EntityImportService(db)

    # 复用映射模板：请求里显式给的计划优先，没给的由模板补齐（不覆盖用户选择）
    if payload.template_id is not None:
        template = await db.get(ImportMappingTemplate, payload.template_id)
        if template is None:
            return {"error": "映射模板不存在"}
        if template.kb_id is not None and template.kb_id != payload.kb_id:
            return {"error": "该模板属于其他知识库，不能跨库应用"}
        try:
            config = TemplateConfig.from_dict(template.config or {})
        except TemplateError as exc:
            return {"error": f"映射模板不可用：{exc}"}
        plan = MappingTemplateService.to_plan(config)
        for table, pk in plan["primary_keys"].items():
            payload.primary_keys.setdefault(table, pk)
        for item in plan["relations"]:
            if not any(
                fk.child_table == item["child_table"] and fk.child_field == item["child_field"]
                for fk in payload.foreign_keys
            ):
                payload.foreign_keys.append(RelationPlanItem(**item))
        for item in plan["junctions"]:
            if not any(j.table == item["table"] for j in payload.junctions):
                payload.junctions.append(JunctionPlanItem(**item))

    foreign_keys = [
        ForeignKeyCandidate(
            child_table=item.child_table,
            child_field=item.child_field,
            parent_table=item.parent_table,
            parent_field=item.parent_field,
            name_similarity=1.0,
            coverage=1.0,
            type_match=True,
            confidence=item.confidence,
            cardinality="1:N",
            evidence="用户确认",
            status=KG_STATUS_CONFIRMED if payload.auto_confirm else KG_STATUS_PENDING,
        )
        for item in payload.foreign_keys
    ]
    junctions = [
        JunctionCandidate(
            table=item.table,
            left_table=item.left_table,
            left_field=item.left_field,
            right_table=item.right_table,
            right_field=item.right_field,
            confidence=item.confidence,
            evidence="用户确认",
            relation_type=item.relation_type,
            status=KG_STATUS_CONFIRMED if payload.auto_confirm else KG_STATUS_PENDING,
        )
        for item in payload.junctions
    ]

    stats = await svc.import_relational(
        kb_id=payload.kb_id,
        user=user,
        job_id=job.id,
        rows_by_table=rows_by_table,
        files_by_table=files_by_table,
        primary_keys=payload.primary_keys,
        foreign_keys=foreign_keys,
        junctions=junctions,
        batch_id=job.batch_id,
        auto_confirm=payload.auto_confirm,
    )
    job.processed = sum(len(r) for r in rows_by_table.values())
    job.succeeded = job.processed - stats.failed_rows
    job.failed = stats.failed_rows + len(commit_errors)
    job.status = "completed"

    # 另存为映射模板：把本次「实际生效」的决策固化下来供下次复用
    saved_template: dict[str, Any] | None = None
    if payload.save_as_template:
        config = TemplateConfig(
            tables={
                name: TableMapping(
                    table=name,
                    entity_type=name,
                    pk_field=payload.primary_keys.get(name),
                )
                for name in rows_by_table
            },
            relations=[item.model_dump() for item in payload.foreign_keys],
            junctions=[item.model_dump() for item in payload.junctions],
        )
        fingerprint = dataset_fingerprint(
            {
                name: list(rows[0].data.keys()) if rows else []
                for name, rows in rows_by_table.items()
            }
        )
        template = await MappingTemplateService(db).save(
            kb_id=payload.kb_id,
            name=payload.save_as_template,
            fingerprint=fingerprint,
            config=config,
            user=user,
        )
        saved_template = {"id": str(template.id), "name": template.name}

    await db.commit()
    shutil.rmtree(session_dir, ignore_errors=True)

    # 解析错误（坏行跳过/整文件失败）与入库错误一并返回，失败行数保持一致
    stats_payload = stats.to_dict()
    stats_payload["errors"] = [*stats_payload["errors"], *commit_errors][:50]
    stats_payload["failed_rows"] += len(commit_errors)
    return {"job_id": str(job.id), "template": saved_template, **stats_payload}


# ── 映射模板 ────────────────────────────────────────────────────────────────


class TemplateSaveRequest(BaseModel):
    kb_id: uuid.UUID | None = None
    name: str
    # 二选一：直接给指纹，或给 {表名: [字段名]} 由服务端计算
    fingerprint: str | None = None
    tables: dict[str, list[str]] | None = None
    config: dict[str, Any]


class TemplateMatchRequest(BaseModel):
    kb_id: uuid.UUID | None = None
    tables: dict[str, list[str]]


@router.get("/templates")
async def list_templates(
    request: Request,
    kb_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """列出可用模板（知识库内 + 全局）。"""
    user = _admin(request)
    if kb_id is not None:
        await require_kb_read(db, user, kb_id)
    svc = MappingTemplateService(db)
    rows = await svc.list_templates(kb_id)
    return {
        "items": [
            {
                "id": str(t.id),
                "name": t.name,
                "kb_id": str(t.kb_id) if t.kb_id else None,
                "fingerprint": t.schema_fingerprint,
                "config": t.config,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in rows
        ]
    }


@router.post("/templates")
async def save_template(
    request: Request,
    payload: TemplateSaveRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """保存映射模板（同名同指纹覆盖）。"""
    user = _admin(request)
    if payload.kb_id is not None:
        await require_kb_write(db, user, payload.kb_id)
    if not payload.fingerprint and not payload.tables:
        return {"error": "必须提供 fingerprint 或 tables"}
    fingerprint = payload.fingerprint or dataset_fingerprint(payload.tables or {})
    try:
        template = await MappingTemplateService(db).save(
            kb_id=payload.kb_id,
            name=payload.name,
            fingerprint=fingerprint,
            config=payload.config,
            user=user,
        )
    except TemplateError as exc:
        return {"error": str(exc)}
    await db.commit()
    return {"id": str(template.id), "name": template.name, "fingerprint": fingerprint}


@router.post("/templates/match")
async def match_template(
    request: Request,
    payload: TemplateMatchRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """按当前数据集字段匹配模板，返回可用性与字段差异。"""
    user = _admin(request)
    if payload.kb_id is not None:
        await require_kb_read(db, user, payload.kb_id)
    result = await MappingTemplateService(db).match(payload.kb_id, payload.tables)
    return result.to_dict()


# ── 关系确认 ────────────────────────────────────────────────────────────────


@router.post("/relations/status")
async def set_relations_status(
    request: Request,
    payload: RelationStatusRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """人工确认 / 取消 / 拒绝关系（仅 confirmed 的关系会进入知识图谱）。"""
    user = _admin(request)
    svc = EntityImportService(db)
    updated = await svc.bulk_set_status(
        kb_id=payload.kb_id,
        relation_ids=payload.relation_ids,
        status=payload.status,
        user=user,
    )
    await db.commit()

    synced = {"kg_nodes": 0, "kg_edges": 0}
    if payload.status == KG_STATUS_CONFIRMED:
        nodes, edges = await svc.sync_confirmed_to_kg(payload.kb_id)
        await db.commit()
        synced = {"kg_nodes": nodes, "kg_edges": edges}

    return {"updated": updated, "status": payload.status, **synced}


@router.get("/relations")
async def list_relations(
    request: Request,
    kb_id: uuid.UUID,
    status: str | None = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """按知识库列出关系（供确认界面与 ER 图使用）。"""
    user = _admin(request)
    from src.core.deps import require_kb_read

    await require_kb_read(db, user, kb_id)

    query = select(GenericRelation).where(
        GenericRelation.kb_id == kb_id, GenericRelation.deleted_at.is_(None)
    )
    if status:
        query = query.where(GenericRelation.status == status)
    rows = (await db.execute(query.limit(min(limit, 200)))).scalars().all()

    entity_ids = {r.source_entity_id for r in rows} | {r.target_entity_id for r in rows}
    names: dict[str, str] = {}
    if entity_ids:
        entities = (
            await db.execute(
                select(GenericEntity.id, GenericEntity.name, GenericEntity.entity_type).where(
                    GenericEntity.id.in_(list(entity_ids))
                )
            )
        ).all()
        names = {
            str(e[0]): {"name": e[1], "type": e[2]} for e in entities
        }

    return {
        "items": [
            {
                "id": str(r.id),
                "source": names.get(str(r.source_entity_id)),
                "target": names.get(str(r.target_entity_id)),
                "relation_type": r.relation_type,
                "status": r.status,
                "confidence": r.confidence,
                "provenance": r.provenance,
            }
            for r in rows
        ]
    }


# ── 统计 ────────────────────────────────────────────────────────────────────


@router.get("/stats")
async def import_stats(
    request: Request,
    kb_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """导入后统计：实体、关系（按状态）、原始记录、失败记录。"""
    user = _admin(request)
    from src.core.deps import require_kb_read

    await require_kb_read(db, user, kb_id)

    async def _count(model, *filters) -> int:
        stmt = select(func.count()).select_from(model)
        for f in filters:
            stmt = stmt.where(f)
        return int(await db.scalar(stmt) or 0)

    return {
        "entities": await _count(GenericEntity, GenericEntity.kb_id == kb_id),
        "records": await _count(ImportRecord, ImportRecord.kb_id == kb_id),
        "relations_total": await _count(GenericRelation, GenericRelation.kb_id == kb_id),
        "relations_pending": await _count(
            GenericRelation,
            GenericRelation.kb_id == kb_id,
            GenericRelation.status == KG_STATUS_PENDING,
        ),
        "relations_confirmed": await _count(
            GenericRelation,
            GenericRelation.kb_id == kb_id,
            GenericRelation.status == KG_STATUS_CONFIRMED,
        ),
        "relations_rejected": await _count(
            GenericRelation,
            GenericRelation.kb_id == kb_id,
            GenericRelation.status == KG_STATUS_REJECTED,
        ),
    }


# ── 索引一致性 ──────────────────────────────────────────────────────────────


@router.get("/index/environment")
async def index_environment(request: Request) -> dict:
    """当前后端的索引环境（只读）：环境、数据库、集合与是否允许删除向量。"""
    _admin(request)
    return _index_environment()


@router.get("/index/diff")
async def index_diff(
    request: Request,
    kb_id: uuid.UUID | None = None,
    expected_milvus_collection: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """PG 与 Milvus 的索引差异报告（只读，可直接审阅）。"""
    _admin(request)
    _ensure_expected_collection(expected_milvus_collection)
    await _ensure_kb_read(db, request, kb_id)
    report = await IndexStateService(db).diff(kb_id=kb_id)
    return report.to_dict()


@router.post("/index/apply")
async def index_apply(
    request: Request,
    payload: IndexApplyRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """按报告修复索引状态；删除向量必须 confirmed=True 且在隔离环境。"""
    _admin(request)
    _ensure_expected_collection(payload.expected_milvus_collection)
    await _ensure_kb_read(db, request, payload.kb_id)
    wants_delete = payload.delete_orphans or payload.delete_duplicates
    if wants_delete and not payload.confirmed:
        raise HTTPException(
            status_code=400,
            detail=(
                "删除 Milvus 向量属不可逆操作：请先审查差异报告，"
                "再以 confirmed=True 显式确认"
            ),
        )
    if wants_delete:
        _ensure_destructive_allowed()

    svc = IndexStateService(db)
    report = await svc.diff(kb_id=payload.kb_id)
    try:
        result = await svc.apply(
            report,
            fix_flags=payload.fix_flags,
            delete_orphans=payload.delete_orphans,
            delete_duplicates=payload.delete_duplicates,
            confirmed=payload.confirmed,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    return result.to_dict()


@router.post("/index/rebuild")
async def index_rebuild(
    request: Request,
    payload: IndexRebuildRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """标记缺失向量的文档重建（或重置失败文档重试）。"""
    _admin(request)
    _ensure_expected_collection(payload.expected_milvus_collection)
    await _ensure_kb_read(db, request, payload.kb_id)
    svc = IndexStateService(db)
    if payload.retry_failed:
        count = await svc.retry_failed(payload.kb_id)
        await db.commit()
        return {"retry_failed": count}
    count = await svc.mark_for_rebuild(payload.kb_id)
    await db.commit()
    return {"rebuild_marked": count}
