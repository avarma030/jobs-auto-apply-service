from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class RunSearchCriteriaResponse(BaseModel):
    keywords: list[str] = Field(default_factory=list)
    location: Optional[str] = None
    work_modes: list[str] = Field(default_factory=list)
    job_types: list[str] = Field(default_factory=list)
    experience_levels: list[str] = Field(default_factory=list)
    easy_apply_only: Optional[bool] = None
    remote_only: Optional[bool] = None
    boards: list[str] = Field(default_factory=list)
    max_age_days: Optional[int] = None
    max_age_hours: Optional[int] = None
    max_jobs: Optional[int] = None
    tailor_documents: Optional[bool] = None
    min_match_score: Optional[int] = None


class RunJobSummaryResponse(BaseModel):
    total: int = 0
    pending: int = 0
    applied: int = 0
    skipped: int = 0
    failed: int = 0
    interviewed: int = 0
    offered: int = 0
    rejected: int = 0


class RunJobResponse(BaseModel):
    id: int
    title: str
    company: str
    location: Optional[str] = None
    source_board: str
    url: str
    easy_apply: bool = False
    scraped_at: datetime
    posted_at: Optional[datetime] = None
    application_status: str
    applied_at: Optional[datetime] = None
    match_score: Optional[float] = None
    ats_score: Optional[float] = None
    notes: Optional[str] = None

    model_config = {"from_attributes": True}


class RunEventResponse(BaseModel):
    id: int
    event_type: str
    level: str
    message: Optional[str] = None
    status: Optional[str] = None
    jobs_found: Optional[int] = None
    jobs_applied: Optional[int] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class RunResponse(BaseModel):
    id: str
    status: str
    boards: Optional[str] = None
    keywords: Optional[str] = None
    location: Optional[str] = None
    trigger_type: Optional[str] = None
    search_criteria: Optional[RunSearchCriteriaResponse] = None
    started_at: datetime
    finished_at: Optional[datetime] = None
    jobs_found: int = 0
    jobs_applied: int = 0
    job_summary: RunJobSummaryResponse = Field(default_factory=RunJobSummaryResponse)
    error_message: Optional[str] = None

    model_config = {"from_attributes": True}


class RunDetailResponse(RunResponse):
    jobs: list[RunJobResponse] = Field(default_factory=list)
    events: list[RunEventResponse] = Field(default_factory=list)
