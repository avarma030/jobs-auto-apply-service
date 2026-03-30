from __future__ import annotations

import asyncio

from src.config import settings
from src.database.db import Database
from src.services.run_executor import execute_run_by_id
from src.tasks.celery_app import celery_app


@celery_app.task(name="src.tasks.jobs.run_scrape_task")
def run_scrape_task(run_id: str) -> None:
    async def _run() -> None:
        db = Database(settings.database_url)
        await db.init()
        try:
            await execute_run_by_id(
                run_id,
                db=db,
                worker_id=f"celery:{run_scrape_task.request.id}",
            )
        finally:
            await db.close()

    asyncio.run(_run())
