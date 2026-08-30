"""Celery 应用：broker/backend = Redis（TECH_DESIGN：Redis 作为 Celery broker）。

启动 worker（Windows 需 solo 池）：
  .venv\\Scripts\\python.exe -m celery -A src.infrastructure.celery_app:celery_app worker --pool=solo -l info
"""

from celery import Celery

from src.core.config import settings

celery_app = Celery(
    "knowledge_platform",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["src.application.tasks"],
)
celery_app.conf.update(
    task_track_started=True,
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_acks_late=True,
)
