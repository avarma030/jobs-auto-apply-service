from __future__ import annotations

from pathlib import Path

import httpx
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


class StubLinkedInScraper:
    def __init__(self, *args, **kwargs) -> None:
        self.job = Job(
            title="AI Engineer",
            company="Acme AI",
            location="Frankfurt, Germany",
            description=None,
            url="https://www.linkedin.com/jobs/view/456/",
            source_board="linkedin",
            external_id="456",
            easy_apply=False,
            easy_apply_confident=True,
            tags=["LLM", "Agents"],
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def search(self, _search_filter: JobSearchFilter):
        yield self.job

    async def get_job_details(self, job: Job) -> Job:
        raise httpx.HTTPStatusError(
            "blocked",
            request=httpx.Request("GET", job.url),
            response=httpx.Response(429, request=httpx.Request("GET", job.url)),
        )

    def mark_easy_apply_uncertain(self, job: Job, reason: str) -> Job:
        return job.model_copy(
            update={
                "easy_apply_confident": False,
                "notes": f"easy_apply_unconfirmed: {reason}",
            }
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
async def test_autopilot_surfaces_linkedin_external_apply_jobs_as_excluded(tmp_path: Path) -> None:
    profile = build_profile()
    engine = AutopilotEngine(profile=profile, artifact_dir=tmp_path)

    run = await engine.process_jobs(
        [build_matching_job(easy_apply=False)],
        board="linkedin",
        search_filter=JobSearchFilter(keywords=["project manager"], location="Ireland", max_age_days=1),
    )

    assert run.total_scraped == 1
    assert run.filtered_out_count == 0
    assert run.shortlisted_count == 0
    assert run.excluded_external_count == 1
    assert len(run.excluded_results) == 1
    excluded = run.excluded_results[0]
    assert excluded.excluded_from_active_list is True
    assert excluded.exclusion_reason == "linkedin_external"
    assert excluded.route == ApplicationRoute.LINKEDIN_EXTERNAL
    assert excluded.package.resume_path is None
    assert "external apply flow" in excluded.auto_apply_message


def test_autopilot_scoring_does_not_overreward_generic_stopwords() -> None:
    profile = build_profile()
    engine = AutopilotEngine(profile=profile)
    generic_job = Job(
        title="Customer Success Manager",
        company="Generic Corp",
        location="Remote",
        description=(
            "The team is looking for a strong hands-on partner working across products. "
            "The role is focused on building better customer outcomes while working with the team."
        ),
        url="https://example.com/jobs/555",
        source_board="linkedin",
        external_id="555",
        easy_apply=False,
    )

    score, matched, missing, reasons = engine._score_job(
        generic_job,
        JobSearchFilter(keywords=["customer success"], location="Remote", max_age_days=1),
    )

    assert score < 75
    assert "strong" not in matched
    assert "hands" not in matched


def test_autopilot_scoring_rewards_ai_ml_concept_matches() -> None:
    profile = UserProfile(
        first_name="Akshay",
        last_name="Varma",
        email="akshay@example.com",
        headline="Gen AI Engineer",
        summary=(
            "Generative AI engineer building LLM products, machine learning systems, "
            "software platforms, and evaluation workflows."
        ),
        years_of_experience=4,
        skills=[
            "AI",
            "LLM",
            "Python",
            "LangChain",
            "Agents",
            "Machine learning",
            "Software engineering",
            "Evaluation",
        ],
        work_experience=[
            WorkExperience(
                company="Workhuman",
                title="Senior GenAI Engineer",
                start_date="2024-10",
                description=(
                    "Built production AI assistants, machine learning systems, software products, "
                    "and evaluation workflows."
                ),
            )
        ],
    )
    engine = AutopilotEngine(profile=profile)
    job = Job(
        title="Graduate AI software engineer",
        company="Bending Spoons",
        location="Frankfurt, Germany",
        description=(
            "Build AI software products, machine learning systems, evaluation tooling, "
            "and scalable engineering workflows."
        ),
        url="https://example.com/jobs/ai-software-engineer",
        source_board="linkedin",
        external_id="ai-software-engineer",
        easy_apply=False,
        job_type="full_time",
        skills=["AI", "Machine learning", "Evaluation", "Software engineering"],
        tags=["LLM", "Platform"],
    )

    score, matched, missing, reasons = engine._score_job(
        job,
        JobSearchFilter(keywords=["AI Engineer"], location="Frankfurt", max_age_days=30),
    )

    assert score >= 75
    assert any("concept alignment" in reason for reason in reasons)
    assert "ai" in [value.lower() for value in matched]


@pytest.mark.asyncio
async def test_autopilot_marks_linkedin_detail_fetch_failure_as_unconfirmed_exclusion(tmp_path: Path) -> None:
    profile = UserProfile(
        first_name="Akshay",
        last_name="Varma",
        email="akshay@example.com",
        headline="Gen AI Engineer",
        summary="Generative AI engineer building LLM and agent systems.",
        years_of_experience=4,
        skills=["AI", "LLM", "Python", "LangChain", "Agents"],
        work_experience=[
            WorkExperience(
                company="Workhuman",
                title="Gen AI Engineer",
                start_date="2024-10",
                description="Built LLM, RAG, and agentic AI products in production.",
            )
        ],
    )
    engine = AutopilotEngine(profile=profile, artifact_dir=tmp_path)

    run = await engine.run_search(
        board="linkedin",
        scraper_cls=StubLinkedInScraper,
        search_filter=JobSearchFilter(keywords=["ai engineer"], location="Frankfurt", max_age_days=30),
        limit=20,
        scan_cap=20,
    )

    assert run.total_scraped == 1
    assert run.filtered_out_count == 0
    assert run.shortlisted_count == 0
    assert run.excluded_unconfirmed_count == 1
    assert len(run.excluded_results) == 1
    excluded = run.excluded_results[0]
    assert excluded.exclusion_reason == "easy_apply_unconfirmed"
    assert excluded.job.easy_apply_confident is False
    assert excluded.job.notes and "easy_apply_unconfirmed" in excluded.job.notes
