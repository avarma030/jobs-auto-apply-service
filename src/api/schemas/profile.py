from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel


class ProfileResponse(BaseModel):
    """Raw profile JSON returned as a dict for flexibility."""
    profile: dict[str, Any]


class ProfileUpdate(BaseModel):
    profile: dict[str, Any]


class ResumeUploadResponse(BaseModel):
    resume_path: str
    filename: str
    extracted_profile: dict[str, Any] | None = None  # populated when AI extraction runs
    profile_updated: bool = False                     # True if DB profile was updated
    ai_extraction_enabled: bool = False               # True when ANTHROPIC_API_KEY is configured
    extraction_error: str | None = None               # non-None when key is set but extraction failed
