from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import UploadFile
from sqlalchemy import select

import src.services.runtime_state as runtime_state
from src.api.routers.profile import upload_resume, update_profile
from src.api.schemas.profile import ProfileUpdate
from src.config import settings
from src.database import Database
from src.database.models import BoardAccountCredentialRecord, User
from src.services.secret_crypto import get_secret_fernet
from src.services.user_runtime import (
    build_runtime_profile_data,
    get_board_credentials,
    list_board_account_states,
    upsert_board_session_state,
)


async def _make_db(tmp_path: Path) -> tuple[Database, User]:
    db_path = tmp_path / "test.db"
    db = Database(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    await db.init()
    async with db.session_factory() as session:
        user = User(email="tenant@example.com", hashed_password="hashed")
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return db, user


@pytest.mark.asyncio
async def test_update_profile_moves_board_password_out_of_profile_json(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_encryption_key", "tenant-runtime-test-key")
    get_secret_fernet.cache_clear()
    db, user = await _make_db(tmp_path)
    try:
        async with db.session_factory() as session:
            response = await update_profile(
                ProfileUpdate(
                    profile={
                        "first_name": "Rucha",
                        "last_name": "Varma",
                        "email": "rucha@example.com",
                        "job_board_accounts": {
                            "linkedin": {
                                "username": "rucha@example.com",
                                "password": "super-secret-password",
                            }
                        },
                    }
                ),
                session=session,
                current_user=user,
            )

            row = (
                await session.execute(
                    select(BoardAccountCredentialRecord).where(
                        BoardAccountCredentialRecord.user_id == user.id,
                        BoardAccountCredentialRecord.board == "linkedin",
                    )
                )
            ).scalar_one()

            assert "password" not in json.dumps(response.profile)
            assert response.profile["job_board_accounts"]["linkedin"]["username"] == "rucha@example.com"
            assert response.board_account_states[0].has_secret is True
            assert "super-secret-password" not in row.encrypted_secret_json
    finally:
        await db.close()
        get_secret_fernet.cache_clear()


@pytest.mark.asyncio
async def test_blank_password_update_preserves_existing_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_encryption_key", "tenant-runtime-test-key")
    get_secret_fernet.cache_clear()
    db, user = await _make_db(tmp_path)
    try:
        async with db.session_factory() as session:
            await update_profile(
                ProfileUpdate(
                    profile={
                        "first_name": "Rucha",
                        "last_name": "Varma",
                        "email": "rucha@example.com",
                        "job_board_accounts": {
                            "linkedin": {
                                "username": "rucha@example.com",
                                "password": "preserve-me",
                            }
                        },
                    }
                ),
                session=session,
                current_user=user,
            )
            await update_profile(
                ProfileUpdate(
                    profile={
                        "first_name": "Rucha",
                        "last_name": "Varma",
                        "email": "rucha@example.com",
                        "job_board_accounts": {
                            "linkedin": {
                                "username": "rucha@example.com",
                                "password": "",
                            }
                        },
                    }
                ),
                session=session,
                current_user=user,
            )
            creds = await get_board_credentials(
                session,
                user_id=user.id,
                board="linkedin",
                include_secrets=True,
            )
            assert creds == {
                "username": "rucha@example.com",
                "password": "preserve-me",
            }
    finally:
        await db.close()
        get_secret_fernet.cache_clear()


@pytest.mark.asyncio
async def test_upload_resume_uses_user_scoped_runtime_path_not_global(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_state, "_UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(settings, "resume_path", tmp_path / "global-resume.pdf")
    monkeypatch.setattr(settings, "anthropic_api_key", None)

    db, user = await _make_db(tmp_path)
    try:
        async with db.session_factory() as session:
            response = await upload_resume(
                file=UploadFile(filename="resume.pdf", file=BytesIO(b"%PDF-1.4\nresume")),
                session=session,
                current_user=user,
            )
            expected = runtime_state.user_resume_path(user.id)
            assert response.resume_path == str(expected)
            assert expected.exists()
            assert settings.resume_path.exists() is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_runtime_profile_hydrates_scoped_resume_and_hides_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_encryption_key", "tenant-runtime-test-key")
    monkeypatch.setattr(runtime_state, "_UPLOAD_ROOT", tmp_path / "uploads")
    get_secret_fernet.cache_clear()

    db, user = await _make_db(tmp_path)
    try:
        scoped_resume = runtime_state.user_resume_path(user.id)
        scoped_resume.parent.mkdir(parents=True, exist_ok=True)
        scoped_resume.write_text("resume", encoding="utf-8")

        async with db.session_factory() as session:
            await update_profile(
                ProfileUpdate(
                    profile={
                        "first_name": "Akshay",
                        "last_name": "Varma",
                        "email": "akshay@example.com",
                        "job_board_accounts": {
                            "linkedin": {
                                "username": "akshay@example.com",
                                "password": "scoped-secret",
                            }
                        },
                    }
                ),
                session=session,
                current_user=user,
            )

            public_profile = await build_runtime_profile_data(
                session,
                user_id=user.id,
                profile_data={"first_name": "Akshay", "email": "akshay@example.com"},
                include_secrets=False,
            )
            private_profile = await build_runtime_profile_data(
                session,
                user_id=user.id,
                profile_data={"first_name": "Akshay", "email": "akshay@example.com"},
                include_secrets=True,
            )

            assert public_profile["resume_path"] == str(scoped_resume)
            assert public_profile["job_board_accounts"]["linkedin"] == {
                "username": "akshay@example.com"
            }
            assert private_profile["job_board_accounts"]["linkedin"]["password"] == "scoped-secret"
    finally:
        await db.close()
        get_secret_fernet.cache_clear()


@pytest.mark.asyncio
async def test_board_account_states_surface_scoped_session_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_encryption_key", "tenant-runtime-test-key")
    get_secret_fernet.cache_clear()
    db, user = await _make_db(tmp_path)
    try:
        async with db.session_factory() as session:
            await update_profile(
                ProfileUpdate(
                    profile={
                        "first_name": "Rucha",
                        "last_name": "Varma",
                        "email": "rucha@example.com",
                        "job_board_accounts": {
                            "linkedin": {
                                "username": "rucha@example.com",
                                "password": "secret",
                            }
                        },
                    }
                ),
                session=session,
                current_user=user,
            )
            await upsert_board_session_state(
                session,
                user_id=user.id,
                board="linkedin",
                account_key="scoped-account",
                session_kind="scraper",
                account_username="rucha@example.com",
                auth_state="authenticated",
                challenge_kind=None,
            )
            await session.commit()

            states = await list_board_account_states(session, user_id=user.id)
            assert len(states) == 1
            assert states[0].board == "linkedin"
            assert states[0].username == "rucha@example.com"
            assert states[0].has_secret is True
            assert states[0].auth_state == "authenticated"
    finally:
        await db.close()
        get_secret_fernet.cache_clear()
