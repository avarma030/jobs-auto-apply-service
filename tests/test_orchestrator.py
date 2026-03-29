from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.appliers.base import ApplicationResult
from src.config import settings
from src.models import ApplicationStatus, JobSearchFilter, UserProfile
from src.models.user_profile import ApplicationPreferences, JobBoardAccounts, SocialLinks
from src.orchestrator import Orchestrator


def make_profile() -> UserProfile:
    return UserProfile(
        first_name="Akshay",
        last_name="Varma",
        email="akshay@example.com",
        social_links=SocialLinks(),
        job_board_accounts=JobBoardAccounts(),
        preferences=ApplicationPreferences(),
    )


def make_job_record(job_id: int = 1):
    return SimpleNamespace(
        id=job_id,
        title="ML Engineer",
        company="Example Corp",
        location="Remote",
        description="Build production ML systems.",
        url=f"https://www.linkedin.com/jobs/view/{job_id}",
        source_board="linkedin",
        external_id=str(job_id),
        job_type=None,
        work_mode=None,
        experience_level=None,
        salary_min=None,
        salary_max=None,
        salary_currency="USD",
        skills="[]",
        easy_apply=True,
        posted_at=None,
        scraped_at=datetime.utcnow(),
        application_status=ApplicationStatus.PENDING,
        applied_at=None,
        notes=None,
        match_score=None,
        ats_score=None,
        ats_type=None,
        tailored_resume_path=None,
        cover_letter_path=None,
    )


class FakeDb:
    def __init__(self, records):
        self.records = records
        self.pending_calls: list[dict[str, object]] = []
        self.status_updates: list[dict[str, object]] = []
        self.logged_applications: list[dict[str, object]] = []

    async def get_pending_jobs(self, limit=100, user_id=None, scrape_run_id=None):
        self.pending_calls.append(
            {"limit": limit, "user_id": user_id, "scrape_run_id": scrape_run_id}
        )
        return list(self.records)

    async def update_job_status(self, job_id, status, applied_at=None, notes=None):
        self.status_updates.append(
            {
                "job_id": job_id,
                "status": status,
                "applied_at": applied_at,
                "notes": notes,
            }
        )

    async def log_application(self, job_id, status, confirmation_id=None, message=None, user_id=None):
        self.logged_applications.append(
            {
                "job_id": job_id,
                "status": status,
                "confirmation_id": confirmation_id,
                "message": message,
                "user_id": user_id,
            }
        )

    async def update_job_ai_fields(self, *args, **kwargs):
        raise AssertionError("AI fields should not be updated when tailoring is disabled")

    async def update_profile_custom_answers(self, *args, **kwargs):
        raise AssertionError("No new answers should be learned in this test")


class StubApplier:
    def __init__(self):
        self.calls: list[dict[str, object]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def apply(self, job, tailored_resume_path=None, cover_letter=None):
        self.calls.append(
            {
                "job": job,
                "tailored_resume_path": tailored_resume_path,
                "cover_letter": cover_letter,
            }
        )
        return ApplicationResult(job, ApplicationStatus.APPLIED, message="submitted")


@pytest.mark.asyncio
async def test_run_full_pipeline_applies_only_current_run_when_tailoring_disabled(monkeypatch):
    record = make_job_record()
    db = FakeDb([record])
    orch = Orchestrator(profile=make_profile(), db=db)
    applier = StubApplier()

    orch.run_scrape = AsyncMock(return_value=1)
    monkeypatch.setattr(orch, "_pick_applier", lambda job: applier)
    monkeypatch.setattr(settings, "dry_run", False)

    counts = await orch.run_full_pipeline(
        JobSearchFilter(keywords=["ml engineer"]),
        user_id=7,
        run_id="run-123",
        tailor_documents=False,
    )

    orch.run_scrape.assert_awaited_once()
    assert db.pending_calls == [
        {
            "limit": settings.max_applications_per_run,
            "user_id": 7,
            "scrape_run_id": "run-123",
        }
    ]
    assert len(applier.calls) == 1
    assert applier.calls[0]["tailored_resume_path"] is None
    assert applier.calls[0]["cover_letter"] is None
    assert counts["scraped"] == 1
    assert counts["applied"] == 1
    assert counts["failed"] == 0
    assert db.status_updates[0]["status"] == ApplicationStatus.APPLIED
    assert db.logged_applications[0]["status"] == ApplicationStatus.APPLIED
