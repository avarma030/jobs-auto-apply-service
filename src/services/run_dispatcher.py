from __future__ import annotations

from loguru import logger

from src.api.schemas.jobs import ScrapeRequest
from src.config import settings
from src.database.db import Database
from src.services.local_run_worker import LocalRunWorker


async def dispatch_scrape_run(
    run_id: str,
    user_id: int,
    req: ScrapeRequest,
    *,
    db: Database | None = None,
    local_worker: LocalRunWorker | None = None,
) -> None:
    owns_db = False
    if db is None:
        db = Database(settings.database_url)
        await db.init()
        owns_db = True

    try:
        enqueued = await db.enqueue_run_execution(
            run_id,
            user_id=user_id,
            request_payload=req.model_dump(mode="json"),
        )
        if not enqueued:
            logger.info(f"[RunDispatcher] Run {run_id} already queued or claimed; skipping duplicate dispatch")
            return

        if settings.use_task_queue:
            from src.tasks.jobs import run_scrape_task

            await db.mark_run_dispatched(run_id)
            logger.info(f"[RunDispatcher] Queueing durable run {run_id} for user {user_id}")
            run_scrape_task.delay(run_id)
            return

        if local_worker is None:
            raise RuntimeError("Local run worker is not available for durable execution")

        logger.info(f"[RunDispatcher] Enqueued durable local run {run_id} for user {user_id}")
        local_worker.notify()
    finally:
        if owns_db:
            await db.close()
