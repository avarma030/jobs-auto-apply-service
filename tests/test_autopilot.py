from __future__ import annotations

from pathlib import Path

import pytest

from src.appliers.base import ApplicationResult, BaseApplier
from src.autopilot import AutopilotEngine
from src.models import (
    Address,
    ApplicationPackage,
    ApplicationRoute,
    ApplicationStatus,
    AutomationState,
    Job,
    JobSearchFilter,
    SocialLinks,
    UserProfile,
    WorkExperience,
)


def build_profile() -> UserProfile:
    return UserProfile(
        first_name="Jane",
        last_name="Doe",
        email="jane@example.com",
        phone="+353-1-555-0100",
        address=Address(city="Dublin", state="D", zip_code="D01", country="IE"),
        headline="Senior Project Manager",
        summary="Project and delivery leader with deep experience in stakeholder management, agile delivery, budgeting, and programme execution.",
        years_of_experience=8,
        skills=[
            "Project Management",
            "Programme Delivery",
            "Stakeholder Management",
            "Agile",
            "Risk Management",
            "Budgeting",
        ],
        social_links=SocialLinks(linkedin="https://linkedin.com/in/janedoe"),
        work_experience=[
            WorkExperience(
                company="Acme Delivery",
                title="Senior Project Manager",
                start_date="2021-01",
                description="Led programme delivery, stakeholder management, budgeting, and agile execution across multiple teams.",
            )
        ],
    )


def build_matching_job(*, board: str = "linkedin", easy_apply: bool = True) -> Job:
    return Job(
        title="Senior Project Manager",
        company="Acme Delivery",
        location="Dublin, Ireland",
        description=(
            "Lead programme delivery, stakeholder management, agile planning, budgeting, risk management, "
            "and executive communication for a major transformation."
        ),
        url="https://example.com/jobs/123" if board == "linkedin" else "https://acme.wd5.myworkdayjobs.com/job/R-100",
        source_board=board,
        external_id="123",
        job_type="contract",
        experience_level="senior",
        easy_apply=easy_apply,
        skills=["Project Management", "Stakeholder Management", "Agile", "Budgeting"],
        tags=["Transformation", "Delivery"],
    )


@pytest.mark.asyncio
async def test_autopilot_filters_low_match_jobs_and_builds_package(tmp_path: Path) -> None:
    profile = build_profile()
    engine = AutopilotEngine(profile=profile, artifact_dir=tmp_path)

    weak_job = Job(
        title="Staff Nurse",
        company="City Hospital",
        location="Galway, Ireland",
        description="Provide patient care, triage support, and clinical documentation.",
        url="https://example.com/jobs/999",
        source_board="linkedin",
        external_id="999",
        easy_apply=False,
    )

    run = await engine.process_jobs(
        [build_matching_job(), weak_job],
        board="linkedin",
        search_filter=JobSearchFilter(keywords=["project manager"], location="Ireland", max_age_days=1),
    )

    assert run.total_scraped == 2
    assert run.filtered_out_count == 1
    assert run.shortlisted_count == 1

    result = run.results[0]
    assert result.compatibility_score >= 75
    assert result.ats_score >= 90
    assert result.route == ApplicationRoute.LINKEDIN_EASY_APPLY
    assert result.automation_state == AutomationState.QUEUED
    assert result.handler_name == "LinkedInApplier"
    assert result.package.resume_path and result.package.resume_path.exists()
    assert result.package.resume_path.suffix == ".docx"
    assert result.package.cover_letter_path and result.package.cover_letter_path.exists()
    assert run.manifest_path and run.manifest_path.exists()


class StubLinkedInApplier(BaseApplier):
    board_name = "LinkedIn"
    board_slug = "linkedin"

    async def apply(self, job: Job, package: ApplicationPackage | None = None) -> ApplicationResult:
        assert package is not None
        assert package.ats_score >= 90
        return ApplicationResult(
            job,
            ApplicationStatus.APPLIED,
            message="Application submitted through LinkedInApplier.",
            confirmation_id="LI-123",
        )


@pytest.mark.asyncio
async def test_autopilot_auto_applies_when_live_route_is_enabled(tmp_path: Path) -> None:
    profile = build_profile()
    engine = AutopilotEngine(
        profile=profile,
        artifact_dir=tmp_path,
        applier_classes=[StubLinkedInApplier],
        live_apply_routes={ApplicationRoute.LINKEDIN_EASY_APPLY},
    )

    run = await engine.process_jobs(
        [build_matching_job()],
        board="linkedin",
        search_filter=JobSearchFilter(keywords=["project manager"], location="Ireland", max_age_days=1),
    )

    assert run.auto_applied_count == 1
    result = run.results[0]
    assert result.automation_state == AutomationState.APPLIED
    assert result.application_status == ApplicationStatus.APPLIED
    assert result.confirmation_id == "LI-123"
    assert "Application submitted through LinkedInApplier." in result.auto_apply_message


@pytest.mark.asyncio
async def test_autopilot_routes_workday_jobs_to_workday_handler(tmp_path: Path) -> None:
    profile = build_profile()
    engine = AutopilotEngine(profile=profile, artifact_dir=tmp_path)

    run = await engine.process_jobs(
        [build_matching_job(board="workday", easy_apply=False)],
        board="workday",
        search_filter=JobSearchFilter(keywords=["project manager"], location="Ireland", max_age_days=1),
    )

    result = run.results[0]
    assert result.route == ApplicationRoute.WORKDAY
    assert result.handler_name == "WorkdayApplier"
    assert result.automation_state == AutomationState.QUEUED
    assert "WorkdayApplier" in result.auto_apply_message


@pytest.mark.asyncio
async def test_autopilot_hides_linkedin_external_apply_jobs(tmp_path: Path) -> None:
    profile = build_profile()
    engine = AutopilotEngine(profile=profile, artifact_dir=tmp_path)

    run = await engine.process_jobs(
        [build_matching_job(easy_apply=False)],
        board="linkedin",
        search_filter=JobSearchFilter(keywords=["project manager"], location="Ireland", max_age_days=1),
    )

    assert run.total_scraped == 1
    assert run.filtered_out_count == 1
    assert run.shortlisted_count == 0
