from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_user, get_session
from src.api.schemas.settings import SettingsResponse, SettingsUpdate
from src.database.models import User, UserSettings

router = APIRouter(prefix="/settings", tags=["settings"])

_DEFAULTS = SettingsResponse().model_dump()


@router.get("", response_model=SettingsResponse)
async def get_settings(
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    row = await _get_or_create(current_user.id, session)
    data = {**_DEFAULTS, **json.loads(row.settings_json)}
    return SettingsResponse(**data)


@router.put("", response_model=SettingsResponse)
async def update_settings(
    body: SettingsUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    row = await _get_or_create(current_user.id, session)
    current = {**_DEFAULTS, **json.loads(row.settings_json)}
    updates = body.model_dump(exclude_none=True)
    current.update(updates)
    row.settings_json = json.dumps(current)
    await session.commit()
    await session.refresh(row)
    return SettingsResponse(**current)


async def _get_or_create(user_id: int, session: AsyncSession) -> UserSettings:
    row = (
        await session.execute(select(UserSettings).where(UserSettings.user_id == user_id))
    ).scalar_one_or_none()
    if row is None:
        row = UserSettings(user_id=user_id, settings_json="{}")
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row
