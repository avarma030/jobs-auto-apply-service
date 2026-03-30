from __future__ import annotations

from loguru import logger

from src.api.schemas.jobs import ScrapeRequest
from src.database.db import Database


async def execute_run_by_id(
    run_id: str,
    *,
    db: Database,
    worker_id: str,
) -> bool:
    claimed = await db.claim_run_execution(run_id, worker_id=worker_id)
    if claimed is None:
        return False

    user_id = int(claimed["user_id"])
    payload = claimed.get("request_payload") or {}

    try:
        req = ScrapeRequest.model_validate(payload)
    except Exception as exc:
        logger.exception(f"[RunExecutor] Invalid run payload for {run_id}: {exc}")
        await db.complete_run_execution(
            run_id,
            worker_id=worker_id,
            state="failed",
            last_error=f"Invalid run payload: {exc}",
        )
        return False

    from src.api.routers.jobs import _run_scrape

    final_status = await _run_scrape(run_id, user_id, req, db=db)
    execution_state = {
        "done": "completed",
        "failed": "failed",
        "stopped": "cancelled",
    }.get(final_status, "completed")
    await db.complete_run_execution(
        run_id,
        worker_id=worker_id,
        state=execution_state,
        last_error=None if execution_state != "failed" else "Run failed",
    )
    return True


async def execute_next_run(
    *,
    db: Database,
    worker_id: str,
) -> bool:
    claimed = await db.claim_next_run_execution(worker_id=worker_id)
    if claimed is None:
        return False

    run_id = str(claimed["run_id"])
    user_id = int(claimed["user_id"])
    payload = claimed.get("request_payload") or {}

    try:
        req = ScrapeRequest.model_validate(payload)
    except Exception as exc:
        logger.exception(f"[RunExecutor] Invalid queued payload for {run_id}: {exc}")
        await db.complete_run_execution(
            run_id,
            worker_id=worker_id,
            state="failed",
            last_error=f"Invalid run payload: {exc}",
        )
        return True

    from src.api.routers.jobs import _run_scrape

    final_status = await _run_scrape(run_id, user_id, req, db=db)
    execution_state = {
        "done": "completed",
        "failed": "failed",
        "stopped": "cancelled",
    }.get(final_status, "completed")
    await db.complete_run_execution(
        run_id,
        worker_id=worker_id,
        state=execution_state,
        last_error=None if execution_state != "failed" else "Run failed",
    )
    return True
