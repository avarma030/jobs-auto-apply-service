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
# Background scrape task
# ---------------------------------------------------------------------------

async def _run_scrape(run_id: str, user_id: int, req: ScrapeRequest) -> None:
    from src.config import settings
    from src.database.db import Database
    from src.models import JobSearchFilter
    from src.orchestrator import SCRAPER_REGISTRY

    db = Database(settings.database_url)
    await db.init()

    async with db.session_factory() as session:
        run = (await session.execute(select(ScrapeRun).where(ScrapeRun.id == run_id))).scalar_one()
        run.status = "running"
        await session.commit()

    try:
        filt = JobSearchFilter(
            keywords=req.keywords,
            location=req.location,
            remote_only=req.remote_only,
            max_age_days=req.max_age_days,
        )
        total = 0
        for board in req.boards:
            if board not in SCRAPER_REGISTRY:
                continue
            scraper_cls = SCRAPER_REGISTRY[board]
            try:
                async with scraper_cls(credentials={}) as scraper:
                    async for job in scraper.search(filt):
                        async with db.session_factory() as session:
                            record = JobRecord(
                                user_id=user_id,
                                scrape_run_id=run_id,
                                external_id=job.external_id,
                                source_board=job.source_board,
                                url=job.url,
                                title=job.title,
                                company=job.company,
                                location=job.location,
                                description=job.description,
                                job_type=job.job_type,
                                work_mode=job.work_mode,
                                experience_level=job.experience_level,
                                salary_min=job.salary_min,
                                salary_max=job.salary_max,
                                salary_currency=job.salary_currency,
                                skills=json.dumps(job.skills),
                                easy_apply=job.easy_apply,
                                posted_at=job.posted_at,
                                scraped_at=job.scraped_at,
                                application_status="pending",
                            )
                            session.add(record)
                            await session.commit()
                        total += 1
            except NotImplementedError:
                pass
            except Exception:
                pass

        async with db.session_factory() as session:
            run = (await session.execute(select(ScrapeRun).where(ScrapeRun.id == run_id))).scalar_one()
            run.status = "done"
            run.jobs_found = total
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
    )
