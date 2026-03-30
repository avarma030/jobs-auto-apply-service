from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


class BoardAccountStateResponse(BaseModel):
    board: str
    username: str | None = None
    has_secret: bool
    auth_state: str = "unknown"
    challenge_kind: str | None = None
    last_validated_at: datetime | None = None
    last_success_at: datetime | None = None


class ProfileResponse(BaseModel):
    """Raw profile JSON returned as a dict for flexibility."""
    profile: dict[str, Any]
    board_account_states: list[BoardAccountStateResponse] = []


class ProfileUpdate(BaseModel):
    profile: dict[str, Any]


class ResumeUploadResponse(BaseModel):
    resume_path: str
    filename: str
    extracted_profile: dict[str, Any] | None = None  # populated when AI extraction runs
    profile_updated: bool = False                     # True if DB profile was updated
    ai_extraction_enabled: bool = False               # True when ANTHROPIC_API_KEY is configured
    extraction_error: str | None = None               # non-None when key is set but extraction failed
