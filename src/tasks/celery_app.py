from __future__ import annotations

from celery import Celery

from src.config import settings


celery_app = Celery(
    "jobs_auto_apply",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
)

celery_app.autodiscover_tasks(["src.tasks"])

