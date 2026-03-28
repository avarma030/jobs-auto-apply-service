from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_user, get_session
from src.api.schemas.jobs import JobResponse, JobStatusUpdate, JobsPage, ScrapeRequest
from src.database.models import JobRecord, ScrapeRun, User

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("", response_model=JobsPage)
async def list_jobs(
    status: Optional[str] = None,
    board: Optional[str] = None,
    keywords: Optional[str] = None,
    page: int = 1,
    page_size: int = 25,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    q = select(JobRecord).where(JobRecord.user_id == current_user.id)
    if status:
        q = q.where(JobRecord.application_status == status)
    if board:
        q = q.where(JobRecord.source_board == board)
    if keywords:
        q = q.where(JobRecord.title.ilike(f"%{keywords}%"))

    count_q = select(func.count()).select_from(q.subquery())
    total = (await session.execute(count_q)).scalar_one()

    q = q.order_by(JobRecord.scraped_at.desc()).offset((page - 1) * page_size).limit(page_size)
    records = list((await session.execute(q)).scalars().all())

    items = [_to_response(r) for r in records]
    return JobsPage(items=items, total=total, page=page, page_size=page_size)


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: int,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    r = await _get_job_or_404(job_id, current_user.id, session)
    return _to_response(r)


@router.put("/{job_id}/status", response_model=JobResponse)
async def update_job_status(
    job_id: int,
    body: JobStatusUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    r = await _get_job_or_404(job_id, current_user.id, session)
    r.application_status = body.status
    await session.commit()
    await session.refresh(r)
    return _to_response(r)


@router.post("/scrape", status_code=202)
async def trigger_scrape(
    body: ScrapeRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    run = ScrapeRun(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        status="pending",
        boards=",".join(body.boards),
        keywords=",".join(body.keywords),
        location=body.location,
        started_at=datetime.utcnow(),
    )
    session.add(run)
    await session.commit()
    background_tasks.add_task(_run_scrape, run.id, current_user.id, body)
    return {"run_id": run.id}


# ---------------------------------------------------------------------------
# Background pipeline task
# ---------------------------------------------------------------------------

async def _run_scrape(run_id: str, user_id: int, req: ScrapeRequest) -> None:
    """Run the full AI pipeline (scrape → score → tailor → apply) as a background task.

    Profile is loaded from the database for the triggering user.
    Progress messages are pushed to the SSE stream via append_run_progress().
    """
    import json as _json

    from src.api.routers.runs import append_run_progress
    from src.config import settings
    from src.database.db import Database
    from src.database.models import UserProfile as UserProfileRecord
    from src.models import JobSearchFilter, UserProfile
    from src.orchestrator import Orchestrator

    db = Database(settings.database_url)
    await db.init()

    async with db.session_factory() as session:
        run = (await session.execute(select(ScrapeRun).where(ScrapeRun.id == run_id))).scalar_one()
        run.status = "running"
        await session.commit()

    def _progress(msg: str) -> None:
        append_run_progress(run_id, msg)

    try:
        filt = JobSearchFilter(
            keywords=req.keywords,
            location=req.location,
            remote_only=req.remote_only,
            max_age_days=req.max_age_days,
        )

        # Load profile from DB for this user; fall back to sensible defaults so
        # the pipeline always runs even for a freshly-registered user.
        async with db.session_factory() as session:
            row = (
                await session.execute(
                    select(UserProfileRecord).where(UserProfileRecord.user_id == user_id)
                )
            ).scalar_one_or_none()
        profile_data: dict = _json.loads(row.profile_json) if (row and row.profile_json) else {}
        profile_data.setdefault("first_name", "User")
        profile_data.setdefault("last_name", "")
        profile_data.setdefault("email", "")
        try:
            profile = UserProfile(**profile_data)
        except Exception:
            profile = UserProfile.model_construct(
                first_name=profile_data.get("first_name", "User"),
                last_name=profile_data.get("last_name", ""),
                email=profile_data.get("email", ""),
            )

        orch = Orchestrator(profile=profile, db=db)

        # Always run the full pipeline. The orchestrator degrades gracefully:
        # - no ANTHROPIC_API_KEY → scoring/tailoring skipped, jobs remain pending
        # - no resume file      → scoring skipped, jobs remain pending
        # - both present        → full scrape → score → tailor → apply flow
        counts = await orch.run_full_pipeline(
            filt,
            user_id=user_id,
            progress_callback=_progress,
        )
        total_found = (
            counts.get("applied", 0)
            + counts.get("skipped", 0)
            + counts.get("failed", 0)
            + counts.get("dry_run", 0)
        )
        total_applied = counts.get("applied", 0)

        async with db.session_factory() as session:
            run = (await session.execute(select(ScrapeRun).where(ScrapeRun.id == run_id))).scalar_one()
            run.status = "done"
            run.jobs_found = total_found
            run.jobs_applied = total_applied
            run.finished_at = datetime.utcnow()
            await session.commit()

    except Exception as exc:
        async with db.session_factory() as session:
            run = (await session.execute(select(ScrapeRun).where(ScrapeRun.id == run_id))).scalar_one()
            run.status = "failed"
            run.error_message = str(exc)
            run.finished_at = datetime.utcnow()
            await session.commit()
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_job_or_404(job_id: int, user_id: int, session: AsyncSession) -> JobRecord:
    r = (
        await session.execute(
            select(JobRecord).where(JobRecord.id == job_id, JobRecord.user_id == user_id)
        )
    ).scalar_one_or_none()
    if r is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return r


def _to_response(r: JobRecord) -> JobResponse:
    skills: list[str] = []
    if r.skills:
        try:
            skills = json.loads(r.skills)
        except Exception:
            pass
    return JobResponse(
        id=r.id,
        title=r.title,
        company=r.company,
        location=r.location,
        source_board=r.source_board,
        url=r.url,
        job_type=r.job_type,
        work_mode=r.work_mode,
        experience_level=r.experience_level,
        salary_min=r.salary_min,
        salary_max=r.salary_max,
        salary_currency=r.salary_currency,
        easy_apply=r.easy_apply,
        posted_at=r.posted_at,
        scraped_at=r.scraped_at,
        application_status=r.application_status,
        applied_at=r.applied_at,
        skills=skills,
        match_score=getattr(r, "match_score", None),
        ats_score=getattr(r, "ats_score", None),
        ats_type=getattr(r, "ats_type", None),
        tailored_resume_path=getattr(r, "tailored_resume_path", None),
        cover_letter_path=getattr(r, "cover_letter_path", None),
    )
