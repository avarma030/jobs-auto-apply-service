from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class JobResponse(BaseModel):
    id: int
    title: str
    company: str
    location: Optional[str] = None
    description: Optional[str] = None
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
    skills: list[str] = Field(default_factory=list)
    # AI pipeline fields
    match_score: Optional[float] = None
    ats_score: Optional[float] = None
    ats_type: Optional[str] = None
    tailored_resume_path: Optional[str] = None
    cover_letter_path: Optional[str] = None

    model_config = {"from_attributes": True}


class SearchCriteria(BaseModel):
    keywords: list[str] = Field(default_factory=lambda: ["software engineer"])
    location: Optional[str] = None
    # Work mode filters - one or more of: "remote", "hybrid", "onsite"
    work_modes: list[str] = Field(default_factory=list)
    # Job type filters - one or more of: "full_time", "part_time", "contract", "internship", "temporary"
    job_types: list[str] = Field(default_factory=list)
    # Experience level filters - one or more of: "entry", "mid", "senior", "lead", "executive"
    experience_levels: list[str] = Field(default_factory=list)
    # Easy Apply only (LinkedIn one-click apply)
    easy_apply_only: bool = False
    # Legacy shortcut - if True, forces work_modes = ["remote"]
    remote_only: bool = False
    boards: list[str] = Field(default_factory=lambda: ["linkedin"])
    max_age_days: int = 7
    # Higher-resolution age filter for jobs posted in the last N hours.
    # When present, this takes precedence over max_age_days.
    max_age_hours: Optional[int] = None
    # Maximum number of jobs to scrape per board (None = no limit)
    max_jobs: Optional[int] = None
    # Enable AI resume / cover-letter tailoring for this run before applying.
    tailor_documents: bool = False
    # Minimum match % to qualify a job for tailoring + apply (None = use server default 75%)
    min_match_score: Optional[int] = None


class ScrapeRequest(SearchCriteria):
    # Save the latest search criteria so it can be replayed automatically later.
    save_search: bool = True
    # Optional recurring schedule for the saved search.
    saved_search_enabled: Optional[bool] = None
    saved_search_interval_hours: Optional[Literal[1, 3]] = None


class SavedSearchConfig(BaseModel):
    enabled: bool = False
    interval_hours: Literal[1, 3] = 3
    criteria: Optional[SearchCriteria] = None
    last_triggered_at: Optional[datetime] = None
    last_run_id: Optional[str] = None


class SavedSearchRunSummary(BaseModel):
    id: str
    status: str
    trigger_type: Optional[str] = None
    started_at: datetime
    finished_at: Optional[datetime] = None
    jobs_found: int = 0
    jobs_applied: int = 0
    job_summary: dict[str, int] = Field(default_factory=dict)
    error_message: Optional[str] = None


class SavedSearchState(SavedSearchConfig):
    next_trigger_at: Optional[datetime] = None
    run_count: int = 0
    runs: list[SavedSearchRunSummary] = Field(default_factory=list)


class JobStatusUpdate(BaseModel):
    status: str  # approved | skipped | pending


class JobsPage(BaseModel):
    items: list[JobResponse]
    total: int
    page: int
    page_size: int
