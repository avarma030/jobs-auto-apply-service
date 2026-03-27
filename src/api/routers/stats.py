from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_user, get_session
from src.api.schemas.stats import StatsResponse
from src.database.models import JobRecord, User

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("", response_model=StatsResponse)
async def get_stats(
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    uid = current_user.id

    # Counts by application_status
    rows = list(
        (
            await session.execute(
                select(JobRecord.application_status, func.count(JobRecord.id))
                .where(JobRecord.user_id == uid)
                .group_by(JobRecord.application_status)
            )
        ).all()
    )
    by_status: dict[str, int] = {r[0]: r[1] for r in rows}

    # Counts by board
    board_rows = list(
        (
            await session.execute(
                select(JobRecord.source_board, func.count(JobRecord.id))
                .where(JobRecord.user_id == uid)
                .group_by(JobRecord.source_board)
            )
        ).all()
    )
    by_board: dict[str, int] = {r[0]: r[1] for r in board_rows}

    # This-week applied
    week_ago = datetime.utcnow() - timedelta(days=7)
    this_week = (
        await session.execute(
            select(func.count(JobRecord.id)).where(
                JobRecord.user_id == uid,
                JobRecord.application_status == "applied",
                JobRecord.applied_at >= week_ago,
            )
        )
    ).scalar_one()

    applied = by_status.get("applied", 0)
    failed = by_status.get("failed", 0)
    success_rate = round(applied / (applied + failed) * 100, 1) if (applied + failed) > 0 else 0.0

    return StatsResponse(
        total_scraped=sum(by_status.values()),
        total_applied=applied,
        total_skipped=by_status.get("skipped", 0),
        total_failed=failed,
        total_interviewing=by_status.get("interviewing", 0),
        total_offered=by_status.get("offered", 0),
        total_rejected=by_status.get("rejected", 0),
        success_rate=success_rate,
        this_week_applied=this_week or 0,
        by_board=by_board,
    )
