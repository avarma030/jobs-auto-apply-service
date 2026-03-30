from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import BoardAccountCredentialRecord, BoardSessionRecord, UserProfile
from src.services.runtime_state import user_resume_path
from src.services.secret_crypto import decrypt_secret_payload, encrypt_secret_payload


@dataclass
class BoardAccountState:
    board: str
    username: str | None
    has_secret: bool
    auth_state: str
    challenge_kind: str | None
    last_validated_at: datetime | None
    last_success_at: datetime | None


def _clean_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_board_secret_payload(raw_creds: dict[str, Any]) -> dict[str, str]:
    payload: dict[str, str] = {}
    password = _clean_string(raw_creds.get("password"))
    access_token = _clean_string(raw_creds.get("access_token"))
    if password:
        payload["password"] = password
    if access_token:
        payload["access_token"] = access_token
    return payload


def strip_profile_board_secrets(profile_data: dict[str, Any]) -> dict[str, Any]:
    sanitized = json.loads(json.dumps(profile_data or {}))
    raw_accounts = sanitized.get("job_board_accounts")
    if not isinstance(raw_accounts, dict):
        return sanitized
    cleaned_accounts: dict[str, dict[str, str]] = {}
    for raw_board, raw_creds in raw_accounts.items():
        board = _clean_string(raw_board)
        if not board or not isinstance(raw_creds, dict):
            continue
        username = _clean_string(raw_creds.get("username"))
        if username:
            cleaned_accounts[board] = {"username": username}
    if cleaned_accounts:
        sanitized["job_board_accounts"] = cleaned_accounts
    else:
        sanitized.pop("job_board_accounts", None)
    return sanitized


async def migrate_profile_credentials(
    session: AsyncSession,
    user_id: int,
    row: UserProfile,
) -> tuple[dict[str, Any], bool]:
    profile_data = json.loads(row.profile_json or "{}")
    migrated = False
    raw_accounts = profile_data.get("job_board_accounts")
    if isinstance(raw_accounts, dict):
        for raw_board, raw_creds in raw_accounts.items():
            board = _clean_string(raw_board)
            if not board or not isinstance(raw_creds, dict):
                continue
            username = _clean_string(raw_creds.get("username"))
            secret_payload = _clean_board_secret_payload(raw_creds)
            if username or secret_payload:
                await upsert_board_credentials(
                    session,
                    user_id=user_id,
                    board=board,
                    username=username,
                    secret_payload=secret_payload,
                )
                if secret_payload:
                    migrated = True
    sanitized = strip_profile_board_secrets(profile_data)
    if sanitized != profile_data:
        row.profile_json = json.dumps(sanitized)
        migrated = True
    return sanitized, migrated


async def upsert_board_credentials(
    session: AsyncSession,
    *,
    user_id: int,
    board: str,
    username: str | None,
    secret_payload: dict[str, str] | None,
) -> BoardAccountCredentialRecord | None:
    normalized_board = board.strip().lower()
    normalized_username = _clean_string(username)
    row = await session.get(
        BoardAccountCredentialRecord,
        {"user_id": user_id, "board": normalized_board},
    )
    incoming_secret_payload = {
        key: value
        for key, value in (secret_payload or {}).items()
        if _clean_string(value)
    }
    existing_secret_payload = decrypt_secret_payload(
        row.encrypted_secret_json if row is not None else None
    )

    if row is None and not normalized_username and not incoming_secret_payload:
        return None

    if row is None:
        row = BoardAccountCredentialRecord(user_id=user_id, board=normalized_board)
        session.add(row)

    previous_username = _clean_string(row.username)
    username_changed = previous_username != normalized_username
    if incoming_secret_payload:
        effective_secret_payload = incoming_secret_payload
    elif username_changed:
        effective_secret_payload = {}
    else:
        effective_secret_payload = existing_secret_payload

    if not normalized_username and not effective_secret_payload:
        await session.delete(row)
        return None

    row.username = normalized_username
    row.encrypted_secret_json = (
        encrypt_secret_payload(effective_secret_payload)
        if effective_secret_payload
        else None
    )
    return row


async def get_board_credentials(
    session: AsyncSession,
    *,
    user_id: int,
    board: str,
    include_secrets: bool = False,
) -> dict[str, str] | None:
    row = await session.get(
        BoardAccountCredentialRecord,
        {"user_id": user_id, "board": board.strip().lower()},
    )
    if row is None:
        return None
    payload: dict[str, str] = {}
    if row.username:
        payload["username"] = row.username
    if include_secrets and row.encrypted_secret_json:
        payload.update(decrypt_secret_payload(row.encrypted_secret_json))
    return payload or None


async def list_board_credentials(
    session: AsyncSession,
    *,
    user_id: int,
    include_secrets: bool = False,
) -> dict[str, dict[str, str]]:
    rows = list(
        (
            await session.execute(
                select(BoardAccountCredentialRecord).where(
                    BoardAccountCredentialRecord.user_id == user_id
                )
            )
        ).scalars().all()
    )
    result: dict[str, dict[str, str]] = {}
    for row in rows:
        payload: dict[str, str] = {}
        if row.username:
            payload["username"] = row.username
        if include_secrets and row.encrypted_secret_json:
            payload.update(decrypt_secret_payload(row.encrypted_secret_json))
        if payload:
            result[row.board] = payload
    return result


async def build_runtime_profile_data(
    session: AsyncSession,
    *,
    user_id: int,
    profile_data: dict[str, Any],
    include_secrets: bool,
) -> dict[str, Any]:
    runtime_profile = strip_profile_board_secrets(profile_data)
    board_credentials = await list_board_credentials(
        session,
        user_id=user_id,
        include_secrets=include_secrets,
    )
    if board_credentials:
        runtime_profile["job_board_accounts"] = board_credentials
    resume = user_resume_path(user_id)
    if resume.exists():
        runtime_profile["resume_path"] = str(resume)
    return runtime_profile


async def upsert_board_session_state(
    session: AsyncSession,
    *,
    user_id: int,
    board: str,
    account_key: str,
    session_kind: str,
    account_username: str | None = None,
    cookie_path: str | None = None,
    session_path: str | None = None,
    auth_state: str | None = None,
    challenge_kind: str | None = None,
    last_validated_at: datetime | None = None,
    last_success_at: datetime | None = None,
    last_error: str | None = None,
) -> BoardSessionRecord:
    row = (
        await session.execute(
            select(BoardSessionRecord).where(
                BoardSessionRecord.user_id == user_id,
                BoardSessionRecord.board == board,
                BoardSessionRecord.account_key == account_key,
                BoardSessionRecord.session_kind == session_kind,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = BoardSessionRecord(
            user_id=user_id,
            board=board,
            account_key=account_key,
            session_kind=session_kind,
        )
        session.add(row)
    row.account_username = _clean_string(account_username)
    if cookie_path is not None:
        row.cookie_path = cookie_path
    if session_path is not None:
        row.session_path = session_path
    if auth_state is not None:
        row.auth_state = auth_state
    row.challenge_kind = challenge_kind
    if last_validated_at is not None:
        row.last_validated_at = last_validated_at
    if last_success_at is not None:
        row.last_success_at = last_success_at
    row.last_error = _clean_string(last_error)
    return row


async def list_board_account_states(
    session: AsyncSession,
    *,
    user_id: int,
) -> list[BoardAccountState]:
    credential_rows = list(
        (
            await session.execute(
                select(BoardAccountCredentialRecord).where(
                    BoardAccountCredentialRecord.user_id == user_id
                )
            )
        ).scalars().all()
    )
    session_rows = list(
        (
            await session.execute(
                select(BoardSessionRecord).where(BoardSessionRecord.user_id == user_id)
            )
        ).scalars().all()
    )
    by_board: dict[str, BoardAccountState] = {}
    for row in credential_rows:
        by_board[row.board] = BoardAccountState(
            board=row.board,
            username=row.username,
            has_secret=bool(row.encrypted_secret_json),
            auth_state="unknown",
            challenge_kind=None,
            last_validated_at=None,
            last_success_at=None,
        )
    for row in session_rows:
        state = by_board.get(row.board)
        if state is None:
            state = BoardAccountState(
                board=row.board,
                username=row.account_username,
                has_secret=False,
                auth_state=row.auth_state,
                challenge_kind=row.challenge_kind,
                last_validated_at=row.last_validated_at,
                last_success_at=row.last_success_at,
            )
            by_board[row.board] = state
            continue
        if not state.username and row.account_username:
            state.username = row.account_username
        if (
            state.last_validated_at is None
            or (row.last_validated_at and row.last_validated_at >= state.last_validated_at)
        ):
            state.auth_state = row.auth_state
            state.challenge_kind = row.challenge_kind
            state.last_validated_at = row.last_validated_at
        if row.last_success_at and (
            state.last_success_at is None or row.last_success_at >= state.last_success_at
        ):
            state.last_success_at = row.last_success_at
    return [by_board[key] for key in sorted(by_board)]
