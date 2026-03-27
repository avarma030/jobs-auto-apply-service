from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class Address(BaseModel):
    street: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip_code: Optional[str] = None
    country: str = "US"


class WorkExperience(BaseModel):
    company: str
    title: str
    start_date: str  # "YYYY-MM"
    end_date: Optional[str] = None  # None means current
    description: Optional[str] = None
    location: Optional[str] = None


class Education(BaseModel):
    institution: str
    degree: str
    field_of_study: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    gpa: Optional[float] = None


class SocialLinks(BaseModel):
    linkedin: Optional[str] = None
    github: Optional[str] = None
    portfolio: Optional[str] = None
    twitter: Optional[str] = None


class JobBoardCredentials(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None
    # OAuth tokens (stored encrypted at rest)
    access_token: Optional[str] = None


class JobBoardAccounts(BaseModel):
    linkedin: Optional[JobBoardCredentials] = None
    indeed: Optional[JobBoardCredentials] = None
    glassdoor: Optional[JobBoardCredentials] = None
    ziprecruiter: Optional[JobBoardCredentials] = None
    monster: Optional[JobBoardCredentials] = None
    dice: Optional[JobBoardCredentials] = None
    lever: Optional[JobBoardCredentials] = None
    greenhouse: Optional[JobBoardCredentials] = None
    workday: Optional[JobBoardCredentials] = None


class ApplicationPreferences(BaseModel):
    auto_apply: bool = True
    require_confirmation: bool = False  # pause and ask before each apply
    max_applications_per_day: int = 50
    easy_apply_only: bool = False
    min_match_score: int = Field(default=75, ge=0, le=100)
    min_ats_score: int = Field(default=90, ge=0, le=100)
    skip_if_salary_not_listed: bool = False
    preferred_work_modes: list[str] = Field(default_factory=lambda: ["remote", "hybrid"])
    blacklisted_companies: list[str] = Field(default_factory=list)
    whitelisted_companies: list[str] = Field(default_factory=list)


class UserProfile(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Personal info
    first_name: str
    last_name: str
    email: str
    phone: Optional[str] = None
    address: Optional[Address] = None

    # Professional
    headline: Optional[str] = None  # e.g. "Senior Software Engineer"
    summary: Optional[str] = None   # professional summary / bio
    years_of_experience: Optional[int] = None
    skills: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=lambda: ["English"])

    # History
    work_experience: list[WorkExperience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)

    # Links
    social_links: SocialLinks = Field(default_factory=SocialLinks)

    # Files
    resume_path: Optional[Path] = None
    cover_letter_template_path: Optional[Path] = None

    # Accounts on job boards
    job_board_accounts: JobBoardAccounts = Field(default_factory=JobBoardAccounts)

    # Application preferences
    preferences: ApplicationPreferences = Field(default_factory=ApplicationPreferences)

    # Custom answers for common application questions
    custom_answers: dict[str, str] = Field(
        default_factory=dict,
        description="Map of question text/key → answer, used for form auto-fill",
    )

