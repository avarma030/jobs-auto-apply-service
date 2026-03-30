from __future__ import annotations

import asyncio

from loguru import logger

from src.api.schemas.jobs import ScrapeRequest
from src.config import settings


async def dispatch_scrape_run(run_id: str, user_id: int, req: ScrapeRequest) -> None:
    if settings.use_task_queue:
        from src.tasks.jobs import run_scrape_task

        logger.info(f"[RunDispatcher] Queueing run {run_id} for user {user_id}")
        run_scrape_task.delay(run_id, user_id, req.model_dump(mode="json"))
        return

    logger.info(f"[RunDispatcher] Launching in-process async task for run {run_id}")
    asyncio.create_task(_run_locally(run_id, user_id, req))


async def _run_locally(run_id: str, user_id: int, req: ScrapeRequest) -> None:
    from src.api.routers.jobs import _run_scrape

    await _run_scrape(run_id, user_id, req)
