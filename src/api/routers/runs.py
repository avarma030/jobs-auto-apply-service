from __future__ import annotations

import asyncio
import json
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_user, get_session
from src.api.schemas.runs import RunResponse
from src.database.models import ScrapeRun, User

router = APIRouter(prefix="/runs", tags=["runs"])

# ---------------------------------------------------------------------------
# In-process progress queue for AI pipeline events
# Keyed by run_id → list of pending progress message strings.
# jobs.py appends to this; _sse_generator drains it.
# ---------------------------------------------------------------------------
_run_progress: dict[str, list[str]] = {}


def append_run_progress(run_id: str, message: str) -> None:
    """Called by the background task to push a progress message into the SSE stream."""
    _run_progress.setdefault(run_id, []).append(message)


@router.get("", response_model=list[RunResponse])
async def list_runs(
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    rows = list(
        (
            await session.execute(
                select(ScrapeRun)
                .where(ScrapeRun.user_id == current_user.id)
                .order_by(ScrapeRun.started_at.desc())
                .limit(50)
            )
        ).scalars().all()
    )
    return [RunResponse.model_validate(r) for r in rows]


@router.get("/{run_id}/stream")
async def stream_run(
    run_id: str,
    token: str | None = None,  # EventSource can't set headers, so accept via query param
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    row = await _get_run_or_404(run_id, current_user.id, session)
    return StreamingResponse(
        _sse_generator(run_id, current_user.id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{run_id}/stop", response_model=RunResponse)
async def stop_run(
    run_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    row = await _get_run_or_404(run_id, current_user.id, session)
    if row.status == "running":
        row.status = "stopped"
        await session.commit()
        await session.refresh(row)
    return RunResponse.model_validate(row)


async def _get_run_or_404(run_id: str, user_id: int, session: AsyncSession) -> ScrapeRun:
    row = (
        await session.execute(
            select(ScrapeRun).where(ScrapeRun.id == run_id, ScrapeRun.user_id == user_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return row


async def _sse_generator(run_id: str, user_id: int) -> AsyncGenerator[str, None]:
    """Poll the DB every 2 seconds and stream SSE events until the run finishes.

    Two event channels:
      • DB poll  — emits jobs_found / jobs_applied counters when they change
      • Progress queue — emits per-step AI pipeline messages immediately
    """
    from src.config import settings
    from src.database.db import Database

    db = Database(settings.database_url)
    await db.init()

    try:
        last_jobs_found = 0
        last_jobs_applied = 0

        for _ in range(600):  # max 20 minutes (600 * 2s) — AI pipeline takes longer
            # 1. Drain progress queue first (low-latency messages)
            messages = _run_progress.pop(run_id, [])
            for msg in messages:
                yield f"data: {json.dumps({'event': 'progress', 'message': msg})}\n\n"

            # 2. Poll DB for counter updates
            async with db.session_factory() as session:
                row = (
                    await session.execute(
                        select(ScrapeRun).where(ScrapeRun.id == run_id, ScrapeRun.user_id == user_id)
                    )
                ).scalar_one_or_none()

            if row is None:
                break

            if row.jobs_found != last_jobs_found or row.jobs_applied != last_jobs_applied:
                last_jobs_found = row.jobs_found
                last_jobs_applied = row.jobs_applied
                yield f"data: {json.dumps({'event': 'progress', 'status': row.status, 'jobs_found': row.jobs_found, 'jobs_applied': row.jobs_applied})}\n\n"

            if row.status in ("done", "failed", "stopped"):
                # Drain any final messages before closing
                final_messages = _run_progress.pop(run_id, [])
                for msg in final_messages:
                    yield f"data: {json.dumps({'event': 'progress', 'message': msg})}\n\n"
                yield f"data: {json.dumps({'event': row.status, 'jobs_found': row.jobs_found, 'jobs_applied': row.jobs_applied, 'error_message': row.error_message})}\n\n"
                break

            await asyncio.sleep(2)
    finally:
        _run_progress.pop(run_id, None)
        await db.close()
