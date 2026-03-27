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
