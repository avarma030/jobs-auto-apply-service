from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_user, get_session
from src.api.schemas.settings import BoardCapabilityResponse, SettingsResponse, SettingsUpdate
from src.database.models import User, UserSettings
from src.services.board_capabilities import all_board_capabilities, normalize_enabled_boards

router = APIRouter(prefix="/settings", tags=["settings"])

_DEFAULTS = SettingsResponse(
    enabled_boards=normalize_enabled_boards(["linkedin"]),
    supported_boards=normalize_enabled_boards(["linkedin"]),
    board_capabilities=[
        BoardCapabilityResponse(**cap.__dict__) for cap in all_board_capabilities()
    ],
).model_dump()


@router.get("", response_model=SettingsResponse)
async def get_settings(
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    row = await _get_or_create(current_user.id, session)
    data = {**_DEFAULTS, **json.loads(row.settings_json)}
    data["enabled_boards"] = normalize_enabled_boards(data.get("enabled_boards"))
    data["supported_boards"] = normalize_enabled_boards(data.get("supported_boards"))
    data["board_capabilities"] = [
        BoardCapabilityResponse(**cap.__dict__).model_dump()
        for cap in all_board_capabilities()
    ]
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
    if "enabled_boards" in updates:
        updates["enabled_boards"] = normalize_enabled_boards(updates.get("enabled_boards"))
    current.update(updates)
    current["supported_boards"] = normalize_enabled_boards(current.get("supported_boards"))
    current["board_capabilities"] = [
        BoardCapabilityResponse(**cap.__dict__).model_dump()
        for cap in all_board_capabilities()
    ]
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
