from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_user, get_session
from src.api.schemas.runs import (
    RunDetailResponse,
    RunJobResponse,
    RunJobSummaryResponse,
    RunResponse,
    RunSearchCriteriaResponse,
)
from src.database.models import JobRecord, RunJobRecord, ScrapeRun, User

router = APIRouter(prefix="/runs", tags=["runs"])

# ---------------------------------------------------------------------------
# In-process progress queue for AI pipeline events
# Keyed by run_id -> list of pending progress message strings.
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
    summaries = await _job_status_counts_for_runs(
        session,
        current_user.id,
        [row.id for row in rows],
    )
    return [_to_run_response(row, summaries.get(row.id)) for row in rows]


@router.get("/{run_id}", response_model=RunDetailResponse)
async def get_run(
    run_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    row = await _get_run_or_404(run_id, current_user.id, session)
    jobs = (await _jobs_for_runs(session, current_user.id, [run_id])).get(run_id, [])
    summary = _job_summary_from_records(jobs)
    return RunDetailResponse(
        **_to_run_response(row, summary).model_dump(),
        jobs=[RunJobResponse.model_validate(job) for job in jobs],
    )


@router.get("/{run_id}/stream")
async def stream_run(
    run_id: str,
    token: str | None = None,  # EventSource can't set headers, so accept via query param
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    await _get_run_or_404(run_id, current_user.id, session)
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
    summary = (await _job_status_counts_for_runs(session, current_user.id, [row.id])).get(row.id)
    return _to_run_response(row, summary)


async def _get_run_or_404(run_id: str, user_id: int, session: AsyncSession) -> ScrapeRun:
    row = (
        await session.execute(
            select(ScrapeRun).where(ScrapeRun.id == run_id, ScrapeRun.user_id == user_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return row


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _parse_run_search_criteria(row: ScrapeRun) -> RunSearchCriteriaResponse | None:
    if row.search_criteria_json:
        try:
            payload = json.loads(row.search_criteria_json)
            return RunSearchCriteriaResponse.model_validate(payload)
        except Exception:
            pass

    payload: dict[str, object] = {}
    keywords = _split_csv(row.keywords)
    boards = _split_csv(row.boards)
    if keywords:
        payload["keywords"] = keywords
    if boards:
        payload["boards"] = boards
    if row.location:
        payload["location"] = row.location
    if not payload:
        return None
    return RunSearchCriteriaResponse.model_validate(payload)


def _empty_summary() -> RunJobSummaryResponse:
    return RunJobSummaryResponse()


def _finalize_summary(summary: RunJobSummaryResponse) -> RunJobSummaryResponse:
    summary.total = (
        summary.pending
        + summary.applied
        + summary.skipped
        + summary.failed
        + summary.interviewed
        + summary.offered
        + summary.rejected
    )
    return summary


def _job_summary_from_records(records: list[JobRecord]) -> RunJobSummaryResponse:
    summary = _empty_summary()
    for record in records:
        status = (record.application_status or "").lower()
        if hasattr(summary, status):
            setattr(summary, status, getattr(summary, status) + 1)
    return _finalize_summary(summary)


async def _job_status_counts_for_runs(
    session: AsyncSession,
    user_id: int,
    run_ids: list[str],
) -> dict[str, RunJobSummaryResponse]:
    if not run_ids:
        return {}
    jobs_by_run = await _jobs_for_runs(session, user_id, run_ids)
    return {
        run_id: _job_summary_from_records(jobs_by_run.get(run_id, []))
        for run_id in run_ids
    }


async def _jobs_for_runs(
    session: AsyncSession,
    user_id: int,
    run_ids: list[str],
) -> dict[str, list[JobRecord]]:
    if not run_ids:
        return {}

    grouped: dict[str, dict[int, JobRecord]] = {run_id: {} for run_id in run_ids}

    linked_rows = (
        await session.execute(
            select(RunJobRecord.run_id, JobRecord)
            .join(JobRecord, JobRecord.id == RunJobRecord.job_id)
            .where(
                RunJobRecord.run_id.in_(run_ids),
                JobRecord.user_id == user_id,
            )
        )
    ).all()
    for run_id, record in linked_rows:
        grouped.setdefault(run_id, {})[record.id] = record

    legacy_rows = list(
        (
            await session.execute(
                select(JobRecord).where(
                    JobRecord.user_id == user_id,
                    JobRecord.scrape_run_id.in_(run_ids),
                )
            )
        ).scalars().all()
    )
    for record in legacy_rows:
        if record.scrape_run_id is None:
            continue
        grouped.setdefault(record.scrape_run_id, {}).setdefault(record.id, record)

    return {
        run_id: sorted(
            records.values(),
            key=lambda record: record.scraped_at or record.posted_at or datetime.min,
            reverse=True,
        )
        for run_id, records in grouped.items()
    }


def _to_run_response(
    row: ScrapeRun,
    summary: RunJobSummaryResponse | None = None,
) -> RunResponse:
    return RunResponse(
        id=row.id,
        status=row.status,
        boards=row.boards,
        keywords=row.keywords,
        location=row.location,
        trigger_type=row.trigger_type or "manual",
        search_criteria=_parse_run_search_criteria(row),
        started_at=row.started_at,
        finished_at=row.finished_at,
        jobs_found=row.jobs_found,
        jobs_applied=row.jobs_applied,
        job_summary=summary or _empty_summary(),
        error_message=row.error_message,
    )


async def _sse_generator(run_id: str, user_id: int) -> AsyncGenerator[str, None]:
    """Poll the DB every 2 seconds and stream SSE events until the run finishes.

    Two event channels:
      - DB poll  - emits jobs_found / jobs_applied counters when they change
      - Progress queue - emits per-step AI pipeline messages immediately
    """
    from src.config import settings
    from src.database.db import Database

    db = Database(settings.database_url)
    await db.init()

    try:
        last_jobs_found = 0
        last_jobs_applied = 0

        for _ in range(600):  # max 20 minutes (600 * 2s)
            messages = _run_progress.pop(run_id, [])
            for msg in messages:
                yield f"data: {json.dumps({'event': 'progress', 'message': msg})}\n\n"

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
                progress_payload = {
                    "event": "progress",
                    "status": row.status,
                    "jobs_found": row.jobs_found,
                    "jobs_applied": row.jobs_applied,
                }
                yield f"data: {json.dumps(progress_payload)}\n\n"

            if row.status in ("done", "failed", "stopped"):
                final_messages = _run_progress.pop(run_id, [])
                for msg in final_messages:
                    yield f"data: {json.dumps({'event': 'progress', 'message': msg})}\n\n"
                final_payload = {
                    "event": row.status,
                    "jobs_found": row.jobs_found,
                    "jobs_applied": row.jobs_applied,
                    "error_message": row.error_message,
                }
                yield f"data: {json.dumps(final_payload)}\n\n"
                break

            await asyncio.sleep(2)
    finally:
        _run_progress.pop(run_id, None)
        await db.close()
