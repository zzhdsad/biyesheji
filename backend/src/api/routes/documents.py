"""文档管理路由：上传、列表、删除、重新向量化。"""

import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from src.core.config import settings

router = APIRouter(prefix="/documents", tags=["documents"])

# PRD 3.1：支持的主流办公格式
ALLOWED_EXTENSIONS = {"pdf", "docx", "pptx", "xlsx", "txt", "md", "html", "csv"}


@router.get("")
async def list_documents(kb_id: str | None = None, parse_status: str | None = None) -> list[dict]:
    """文档列表，支持按知识库与解析状态筛选。

    TODO: 查询 documents 表，按 kb_id / parse_status 过滤。
    """
    return []


@router.post("/upload")
async def upload_document(
    kb_id: str = Form(...),
    file: UploadFile = File(...),
) -> dict:
    """上传文档：保存原始文件，异步解析由 Celery 任务完成（后续接入）。

    限制：单文件 ≤ MAX_FILE_SIZE_MB；仅支持白名单格式。
    """
    filename = file.filename or ""
    suffix = Path(filename).suffix.lstrip(".").lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型：.{suffix}")

    content = await file.read()
    max_bytes = settings.MAX_FILE_SIZE_MB * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(status_code=400, detail=f"文件超过 {settings.MAX_FILE_SIZE_MB}MB 限制")

    upload_dir = Path(settings.UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)
    doc_id = uuid.uuid4()
    (upload_dir / f"{doc_id}.{suffix}").write_bytes(content)

    # TODO: 写入 documents 表（parse_status=pending）并派发 Celery 解析任务
    return {
        "id": str(doc_id),
        "kb_id": kb_id,
        "file_name": filename,
        "file_size": len(content),
        "parse_status": "pending",
    }


@router.delete("/{doc_id}")
async def delete_document(doc_id: uuid.UUID) -> dict:
    """删除文档及其向量数据。

    TODO: 删除 documents 记录 + Milvus 中对应 chunk + 原始文件。
    """
    return {"id": str(doc_id), "deleted": True}


@router.post("/{doc_id}/reindex")
async def reindex_document(doc_id: uuid.UUID) -> dict:
    """重新向量化。

    TODO: 派发 Celery 任务重新解析并向量化该文档。
    """
    return {"id": str(doc_id), "parse_status": "pending"}
