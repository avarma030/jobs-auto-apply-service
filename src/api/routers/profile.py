from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_user, get_session
from src.api.schemas.profile import (
    BoardAccountStateResponse,
    ProfileResponse,
    ProfileUpdate,
    ResumeUploadResponse,
)
from src.config import settings
from src.database.models import User, UserProfile
from src.services.profile_sanitizer import merge_profile_data, sanitize_profile_data
from src.services.runtime_state import ensure_user_upload_dir, user_resume_path
from src.services.user_runtime import (
    build_runtime_profile_data,
    list_board_account_states,
    migrate_profile_credentials,
    strip_profile_board_secrets,
    upsert_board_credentials,
)

router = APIRouter(prefix="/profile", tags=["profile"])

@router.get("", response_model=ProfileResponse)
async def get_profile(
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    row = await _get_or_create_profile(current_user.id, session)
    _, migrated = await migrate_profile_credentials(session, current_user.id, row)
    if migrated:
        await session.commit()
        await session.refresh(row)
    return await _build_profile_response(session, current_user.id, row)


@router.put("", response_model=ProfileResponse)
async def update_profile(
    body: ProfileUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    row = await _get_or_create_profile(current_user.id, session)
    await migrate_profile_credentials(session, current_user.id, row)

    raw_profile = body.profile if isinstance(body.profile, dict) else {}
    raw_accounts = raw_profile.get("job_board_accounts") if isinstance(raw_profile.get("job_board_accounts"), dict) else None
    if raw_accounts is not None:
        for raw_board, raw_creds in raw_accounts.items():
            if not isinstance(raw_creds, dict):
                continue
            await upsert_board_credentials(
                session,
                user_id=current_user.id,
                board=str(raw_board),
                username=raw_creds.get("username"),
                secret_payload={
                    "password": raw_creds.get("password"),
                    "access_token": raw_creds.get("access_token"),
                },
            )

    sanitized = sanitize_profile_data(strip_profile_board_secrets(raw_profile))
    row.profile_json = json.dumps(sanitized)
    await session.commit()
    await session.refresh(row)
    return await _build_profile_response(session, current_user.id, row)


@router.post("/resume", response_model=ResumeUploadResponse)
async def upload_resume(
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")
    user_dir = ensure_user_upload_dir(current_user.id)
    dest = user_resume_path(current_user.id)
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    extracted: dict | None = None
    profile_updated = False
    extraction_error: str | None = None
    ai_enabled = bool(settings.anthropic_api_key)
    row = await _get_or_create_profile(current_user.id, session)
    existing, migrated = await migrate_profile_credentials(session, current_user.id, row)
    if migrated:
        await session.flush()

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


async def _build_profile_response(
    session: AsyncSession,
    user_id: int,
    row: UserProfile,
) -> ProfileResponse:
    profile_data = json.loads(row.profile_json or "{}")
    runtime_profile = await build_runtime_profile_data(
        session,
        user_id=user_id,
        profile_data=profile_data,
        include_secrets=False,
    )
    board_account_states = await list_board_account_states(session, user_id=user_id)
    return ProfileResponse(
        profile=runtime_profile,
        board_account_states=[
            BoardAccountStateResponse(**state.__dict__) for state in board_account_states
        ],
    )
