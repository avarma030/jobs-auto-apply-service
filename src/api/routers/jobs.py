from __future__ import annotations

import asyncio
import io
import json
import uuid
from datetime import datetime
from typing import Optional

import openpyxl
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_user, get_session
from src.api.schemas.jobs import JobResponse, JobStatusUpdate, JobsPage, ScrapeRequest
from src.database.models import JobRecord, ScrapeRun, User

router = APIRouter(prefix="/jobs", tags=["jobs"])


# ---------------------------------------------------------------------------
# List / read
# ---------------------------------------------------------------------------

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
        # Explicit status filter — show exactly what was requested (including skipped)
        q = q.where(JobRecord.application_status == status)
    else:
        # Default "Active" view — hide skipped (score < 75%) jobs so only
        # qualified jobs are visible unless user explicitly selects "Skipped".
        q = q.where(JobRecord.application_status != "skipped")

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


@router.get("/export")
async def export_jobs(
    status: Optional[str] = None,
    board: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Export jobs as an Excel (.xlsx) file with all available fields."""
    q = select(JobRecord).where(JobRecord.user_id == current_user.id)
    if status:
        q = q.where(JobRecord.application_status == status)
    else:
        q = q.where(JobRecord.application_status != "skipped")
    if board:
        q = q.where(JobRecord.source_board == board)
    q = q.order_by(JobRecord.scraped_at.desc()).limit(10_000)
    records = list((await session.execute(q)).scalars().all())

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Jobs"

    headers = [
        "Title", "Company", "Location", "Board", "Status",
        "Match %", "ATS %", "ATS Type",
        "Job Type", "Work Mode", "Experience Level",
        "Salary Min", "Salary Max", "Currency",
        "Easy Apply", "Skills",
        "Posted Date", "Scraped Date",
        "URL", "Description",
    ]
    ws.append(headers)

    # Bold the header row
    from openpyxl.styles import Font
    for cell in ws[1]:
        cell.font = Font(bold=True)

    for r in records:
        skills_str = ""
        if r.skills:
            try:
                skills_str = ", ".join(json.loads(r.skills))
            except Exception:
                skills_str = r.skills

        ws.append([
            r.title,
            r.company,
            r.location or "",
            r.source_board,
            r.application_status,
            round(r.match_score, 1) if r.match_score is not None else "",
            round(r.ats_score, 1) if r.ats_score is not None else "",
            r.ats_type or "",
            r.job_type or "",
            r.work_mode or "",
            r.experience_level or "",
            r.salary_min if r.salary_min is not None else "",
            r.salary_max if r.salary_max is not None else "",
            r.salary_currency or "",
            "Yes" if r.easy_apply else "No",
            skills_str,
            r.posted_at.strftime("%Y-%m-%d") if r.posted_at else "",
            r.scraped_at.strftime("%Y-%m-%d %H:%M") if r.scraped_at else "",
            r.url,
            r.description or "",
        ])

    # Auto-size columns (approximate)
    for col in ws.columns:
        max_len = max((len(str(cell.value or "")) for cell in col), default=0)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 60)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"jobs_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


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
    from src.models import ExperienceLevel, JobSearchFilter, JobType, UserProfile
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
        # Resolve work modes: prefer work_modes list; fall back to remote_only flag
        work_modes = list(req.work_modes)
        if req.remote_only and "remote" not in work_modes:
            work_modes.append("remote")

        # Convert string values to enums, silently ignoring unknown values
        job_type_enums = []
        for jt in req.job_types:
            try:
                job_type_enums.append(JobType(jt))
            except ValueError:
                pass

        exp_level_enums = []
        for el in req.experience_levels:
            try:
                exp_level_enums.append(ExperienceLevel(el))
            except ValueError:
                pass

        filt = JobSearchFilter(
            keywords=req.keywords,
            location=req.location,
            remote_only="remote" in work_modes and len(work_modes) == 1,
            work_modes=work_modes,
            job_types=job_type_enums,
            experience_levels=exp_level_enums,
            easy_apply_only=req.easy_apply_only,
            max_age_days=req.max_age_days,
            max_jobs=req.max_jobs,
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
            run_id=run_id,
            progress_callback=_progress,
        )
        total_found = counts.get("scraped", (
            counts.get("applied", 0)
            + counts.get("skipped", 0)
            + counts.get("failed", 0)
            + counts.get("dry_run", 0)
        ))
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
        description=r.description,
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
