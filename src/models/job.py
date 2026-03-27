from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class JobType(str, Enum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERNSHIP = "internship"
    TEMPORARY = "temporary"
    FREELANCE = "freelance"


class WorkMode(str, Enum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"


class ExperienceLevel(str, Enum):
    ENTRY = "entry"
    MID = "mid"
    SENIOR = "senior"
    LEAD = "lead"
    EXECUTIVE = "executive"


class ApplicationStatus(str, Enum):
    PENDING = "pending"
    APPLIED = "applied"
    SKIPPED = "skipped"
    FAILED = "failed"
    INTERVIEWED = "interviewed"
    OFFERED = "offered"
    REJECTED = "rejected"


class Job(BaseModel):
    id: Optional[str] = None
    title: str
    company: str
    location: Optional[str] = None
    description: Optional[str] = None
    url: str
    source_board: str  # e.g. "linkedin", "indeed", "glassdoor"
    external_id: Optional[str] = None  # ID on the source board

    job_type: Optional[JobType] = None
    work_mode: Optional[WorkMode] = None
    experience_level: Optional[ExperienceLevel] = None

    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    salary_currency: Optional[str] = "USD"

    skills: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    posted_at: Optional[datetime] = None
    scraped_at: datetime = Field(default_factory=datetime.utcnow)

    easy_apply: bool = False  # supports one-click / easy apply
    requires_cover_letter: bool = False

    # AI pipeline fields
    match_score: Optional[float] = None   # 0-100 resume-job compatibility
    ats_score: Optional[float] = None     # 0-100 ATS score of tailored resume
    tailored_resume_path: Optional[str] = None
    cover_letter_path: Optional[str] = None
    ats_type: Optional[str] = None        # greenhouse, workday, lever, generic …

    application_status: ApplicationStatus = ApplicationStatus.PENDING
    applied_at: Optional[datetime] = None
    notes: Optional[str] = None

    model_config = ConfigDict(use_enum_values=True)


class JobSearchFilter(BaseModel):
    keywords: list[str] = Field(default_factory=list)
    location: Optional[str] = None
    remote_only: bool = False
    job_types: list[JobType] = Field(default_factory=list)
    experience_levels: list[ExperienceLevel] = Field(default_factory=list)
    salary_min: Optional[float] = None
    exclude_keywords: list[str] = Field(default_factory=list)
    max_age_days: int = 7  # only scrape jobs posted within N days
