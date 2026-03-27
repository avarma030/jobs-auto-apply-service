from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_user, get_session
from src.api.schemas.profile import ProfileResponse, ProfileUpdate, ResumeUploadResponse
from src.database.models import User, UserProfile
from src.config import settings

router = APIRouter(prefix="/profile", tags=["profile"])

UPLOAD_DIR = Path("data/uploads")


@router.get("", response_model=ProfileResponse)
async def get_profile(
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    row = await _get_or_create_profile(current_user.id, session)
    return ProfileResponse(profile=json.loads(row.profile_json))


@router.put("", response_model=ProfileResponse)
async def update_profile(
    body: ProfileUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    row = await _get_or_create_profile(current_user.id, session)
    row.profile_json = json.dumps(body.profile)
    await session.commit()
    await session.refresh(row)
    return ProfileResponse(profile=json.loads(row.profile_json))


@router.post("/resume", response_model=ResumeUploadResponse)
async def upload_resume(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")
    user_dir = UPLOAD_DIR / str(current_user.id)
    user_dir.mkdir(parents=True, exist_ok=True)
    dest = user_dir / "resume.pdf"
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    return ResumeUploadResponse(resume_path=str(dest), filename=file.filename)


async def _get_or_create_profile(user_id: int, session: AsyncSession) -> UserProfile:
    row = (
        await session.execute(select(UserProfile).where(UserProfile.user_id == user_id))
    ).scalar_one_or_none()
    if row is None:
        row = UserProfile(user_id=user_id, profile_json="{}")
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row
