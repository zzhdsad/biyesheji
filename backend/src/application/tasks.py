"""Celery 异步任务（TECH_DESIGN：上传后 Celery 任务异步解析 → 状态轮询/SSE 推送进度）。"""

from src.infrastructure.celery_app import celery_app


@celery_app.task(name="documents.parse", bind=True, max_retries=2)
def parse_document_task(self, doc_id: str) -> dict:
    """文档解析任务：解析 → 切片 → 入库。失败自动重试（TECH_DESIGN：失败支持重试）。"""
    from src.application.parse_runner import run_parse

    try:
        return run_parse(doc_id)
    except Exception as exc:
        raise self.retry(exc=exc, countdown=10) from exc
