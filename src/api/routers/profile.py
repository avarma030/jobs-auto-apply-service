from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_user, get_session
from src.api.schemas.profile import ProfileResponse, ProfileUpdate, ResumeUploadResponse
from src.config import settings
from src.database.models import User, UserProfile
from src.services.profile_sanitizer import merge_profile_data, sanitize_profile_data

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
    sanitized = sanitize_profile_data(body.profile)
    row.profile_json = json.dumps(sanitized)
    await session.commit()
    await session.refresh(row)
    await _write_profile_json(sanitized)
    return ProfileResponse(profile=json.loads(row.profile_json))


@router.post("/resume", response_model=ResumeUploadResponse)
async def upload_resume(
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")
    user_dir = UPLOAD_DIR / str(current_user.id)
    user_dir.mkdir(parents=True, exist_ok=True)
    dest = user_dir / "resume.pdf"
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    extracted: dict | None = None
    profile_updated = False
    extraction_error: str | None = None
    ai_enabled = bool(settings.anthropic_api_key)
    row = await _get_or_create_profile(current_user.id, session)
    existing = json.loads(row.profile_json) if row.profile_json and row.profile_json != "{}" else {}

    # Also copy to the global resume path so the orchestrator can find it.
    # The orchestrator tries data/uploads/{user_id}/resume.pdf first (per-user),
    # then falls back to settings.resume_path. Copying here keeps CLI / single-user
    # mode working without any code changes in the orchestrator.
    try:
        settings.resume_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dest, settings.resume_path)
    except Exception as exc:
        logger.warning(f"Could not copy resume to global path: {exc}")

    if ai_enabled:
        try:
            import anthropic as _anthropic
            from src.services import profile_extractor, resume_parser

            resume_text = resume_parser.parse_resume(dest)
            client = _anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
            extracted = await profile_extractor.extract_profile_from_resume(
                resume_text, client, settings.anthropic_model
            )
        except Exception as exc:
            logger.warning(f"Auto-extraction failed after resume upload: {exc}")
            extraction_error = str(exc)

    merged = merge_profile_data(extracted or {}, existing)
    sanitized = sanitize_profile_data(merged, resume_path=str(dest))
    row.profile_json = json.dumps(sanitized)
    await session.commit()
    profile_updated = True
    await _write_profile_json(sanitized)
    if extracted:
        extracted = sanitized

    return ResumeUploadResponse(
        resume_path=str(dest),
        filename=file.filename,
        extracted_profile=extracted,
        profile_updated=profile_updated,
        ai_extraction_enabled=ai_enabled,
        extraction_error=extraction_error,
    )


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


async def _write_profile_json(profile_data: dict) -> None:
    try:
        settings.user_profile_path.parent.mkdir(parents=True, exist_ok=True)
        settings.user_profile_path.write_text(json.dumps(profile_data, indent=2))
        logger.info(f"Profile saved to {settings.user_profile_path}")
    except Exception as exc:
        logger.warning(f"Could not save profile to JSON: {exc}")
