from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_user, get_session
from src.api.schemas.applications import ApplicationResponse, ApplicationUpdate, ApplicationsPage
from src.database.models import ApplicationRecord, JobRecord, User

router = APIRouter(prefix="/applications", tags=["applications"])


@router.get("", response_model=ApplicationsPage)
async def list_applications(
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 25,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    q = select(ApplicationRecord).where(ApplicationRecord.user_id == current_user.id)
    if status:
        q = q.where(ApplicationRecord.status == status)

    total = (await session.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    q = q.order_by(ApplicationRecord.attempted_at.desc()).offset((page - 1) * page_size).limit(page_size)
    records = list((await session.execute(q)).scalars().all())
    return ApplicationsPage(
        items=[ApplicationResponse.model_validate(r) for r in records],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{app_id}", response_model=ApplicationResponse)
async def get_application(
    app_id: int,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    r = await _get_or_404(app_id, current_user.id, session)
    return ApplicationResponse.model_validate(r)


@router.put("/{app_id}", response_model=ApplicationResponse)
async def update_application(
    app_id: int,
    body: ApplicationUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    r = await _get_or_404(app_id, current_user.id, session)
    r.status = body.status
    if body.notes:
        r.message = body.notes
    await session.commit()
    await session.refresh(r)
    return ApplicationResponse.model_validate(r)


async def _get_or_404(app_id: int, user_id: int, session: AsyncSession) -> ApplicationRecord:
    r = (
        await session.execute(
            select(ApplicationRecord).where(
                ApplicationRecord.id == app_id, ApplicationRecord.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if r is None:
        raise HTTPException(status_code=404, detail="Application not found")
    return r
