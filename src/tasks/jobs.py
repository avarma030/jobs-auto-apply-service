from __future__ import annotations

import asyncio

from src.api.schemas.jobs import ScrapeRequest
from src.api.routers.jobs import _run_scrape
from src.tasks.celery_app import celery_app


@celery_app.task(name="src.tasks.jobs.run_scrape_task")
def run_scrape_task(run_id: str, user_id: int, request_payload: dict) -> None:
    req = ScrapeRequest.model_validate(request_payload)
    asyncio.run(_run_scrape(run_id, user_id, req))

