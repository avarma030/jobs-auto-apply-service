from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .job import ApplicationStatus, Job


class ApplicationRoute(str, Enum):
    LINKEDIN_EASY_APPLY = "linkedin_easy_apply"
    LINKEDIN_EXTERNAL = "linkedin_external"
    GREENHOUSE = "greenhouse"
    WORKDAY = "workday"
    LEVER = "lever"
    GENERIC = "generic"


class AutomationState(str, Enum):
    READY = "ready"
    APPLIED = "applied"
    QUEUED = "queued"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class ApplicationPackage(BaseModel):
    resume_path: Path | None = None
    cover_letter_path: Path | None = None
    resume_preview: str = ""
    cover_letter_text: str = ""
    ats_score: int = 0
    matched_keywords: list[str] = Field(default_factory=list)
    added_keywords: list[str] = Field(default_factory=list)

    model_config = ConfigDict(arbitrary_types_allowed=True)


class JobAutomationResult(BaseModel):
    job: Job
    compatibility_score: int
    compatibility_reasons: list[str] = Field(default_factory=list)
    route: ApplicationRoute
    handler_name: str
    package: ApplicationPackage = Field(default_factory=ApplicationPackage)
    automation_state: AutomationState = AutomationState.READY
    application_status: ApplicationStatus = ApplicationStatus.PENDING
    auto_apply_message: str = ""
    confirmation_id: str | None = None
    excluded_from_active_list: bool = False
    exclusion_reason: str | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @property
    def ats_score(self) -> int:
        return self.package.ats_score

    @property
    def matched_keywords(self) -> list[str]:
        return self.package.matched_keywords

    @property
    def added_keywords(self) -> list[str]:
        return self.package.added_keywords


class AutopilotRun(BaseModel):
    board: str
    results: list[JobAutomationResult] = Field(default_factory=list)
    excluded_results: list[JobAutomationResult] = Field(default_factory=list)
    total_scraped: int = 0
    filtered_out_count: int = 0
    excluded_external_count: int = 0
    excluded_unconfirmed_count: int = 0
    auto_applied_count: int = 0
    queued_count: int = 0
    failed_count: int = 0
    ats_blocked_count: int = 0
    manifest_path: Path | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @property
    def shortlisted_count(self) -> int:
        return len(self.results)
