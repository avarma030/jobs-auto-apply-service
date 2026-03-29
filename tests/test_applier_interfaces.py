from __future__ import annotations

import pytest

from src.appliers.generic import GenericApplier
from src.appliers.greenhouse import GreenhouseApplier
from src.appliers.lever import LeverApplier
from src.appliers.workday import WorkdayApplier
from src.models import ApplicationStatus, Job, UserProfile
from src.models.user_profile import ApplicationPreferences, JobBoardAccounts, SocialLinks


def make_profile() -> UserProfile:
    return UserProfile(
        first_name="Akshay",
        last_name="Varma",
        email="akshay@example.com",
        social_links=SocialLinks(),
        job_board_accounts=JobBoardAccounts(),
        preferences=ApplicationPreferences(),
    )


def make_job(url: str, source_board: str) -> Job:
    return Job(
        title="ML Engineer",
        company="Example Corp",
        url=url,
        source_board=source_board,
        external_id="123",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("applier_cls", "job"),
    [
        (GenericApplier, make_job("https://example.com/jobs/123", "generic")),
        (GreenhouseApplier, make_job("https://boards.greenhouse.io/example/jobs/123", "greenhouse")),
        (LeverApplier, make_job("https://jobs.lever.co/example/123", "lever")),
        (WorkdayApplier, make_job("https://example.myworkdayjobs.com/job/123", "workday")),
    ],
)
async def test_placeholder_appliers_accept_pipeline_kwargs(applier_cls, job):
    result = await applier_cls(profile=make_profile()).apply(
        job,
        tailored_resume_path="data/uploads/1/tailored/1/resume.pdf",
        cover_letter="Example cover letter",
    )
    assert result.status == ApplicationStatus.FAILED
    assert "not yet implemented" in result.message.lower()
