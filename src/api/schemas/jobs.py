from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class JobResponse(BaseModel):
    id: int
    title: str
    company: str
    location: Optional[str] = None
    source_board: str
    url: str
    job_type: Optional[str] = None
    work_mode: Optional[str] = None
    experience_level: Optional[str] = None
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    salary_currency: Optional[str] = None
    easy_apply: bool = False
    posted_at: Optional[datetime] = None
    scraped_at: datetime
    application_status: str
    applied_at: Optional[datetime] = None
    skills: list[str] = []
    # AI pipeline fields
    match_score: Optional[float] = None
    ats_score: Optional[float] = None
    ats_type: Optional[str] = None
    tailored_resume_path: Optional[str] = None
    cover_letter_path: Optional[str] = None

    model_config = {"from_attributes": True}


class ScrapeRequest(BaseModel):
    keywords: list[str] = ["software engineer"]
    location: Optional[str] = None
    remote_only: bool = False
    boards: list[str] = ["linkedin"]
    max_age_days: int = 7


class JobStatusUpdate(BaseModel):
    status: str  # approved | skipped | pending


class JobsPage(BaseModel):
    items: list[JobResponse]
    total: int
    page: int
    page_size: int
