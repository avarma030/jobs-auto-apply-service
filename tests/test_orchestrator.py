from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import src.orchestrator as orchestrator_module
from src.appliers.base import ApplicationQuestionPrompt, ApplicationResult
from src.config import settings
from src.models import ApplicationStatus, Job, JobSearchFilter, UserProfile
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
        self.profile_answer_updates: list[dict[str, object]] = []
        self.upserted_jobs: list[dict[str, object]] = []

    async def get_pending_jobs(self, limit=100, user_id=None, scrape_run_id=None):
        self.pending_calls.append(
            {"limit": limit, "user_id": user_id, "scrape_run_id": scrape_run_id}
        )
        if limit is None:
            return list(self.records)
        return list(self.records[:limit])

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

    async def update_profile_custom_answers(self, user_id, custom_answers):
        self.profile_answer_updates.append(
            {"user_id": user_id, "custom_answers": dict(custom_answers)}
        )

    async def upsert_job(self, job, user_id=None, scrape_run_id=None):
        self.upserted_jobs.append(
            {"job": job, "user_id": user_id, "scrape_run_id": scrape_run_id}
        )
        return job


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


class AlreadySubmittedApplier:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def apply(self, job, tailored_resume_path=None, cover_letter=None):
        return ApplicationResult(
            job,
            ApplicationStatus.APPLIED,
            message="Application already submitted for this job",
        )


@pytest.mark.asyncio
async def test_run_full_pipeline_applies_only_current_run_when_tailoring_disabled(monkeypatch):
    record = make_job_record()
    db = FakeDb([record])
    orch = Orchestrator(profile=make_profile(), db=db)
    applier = StubApplier()
    progress_messages: list[str] = []

    orch.run_scrape = AsyncMock(return_value=1)
    monkeypatch.setattr(orch, "_pick_applier", lambda job: applier)
    monkeypatch.setattr(settings, "dry_run", False)

    counts = await orch.run_full_pipeline(
        JobSearchFilter(keywords=["ml engineer"]),
        user_id=7,
        run_id="run-123",
        progress_callback=progress_messages.append,
        tailor_documents=False,
    )

    orch.run_scrape.assert_awaited_once()
    assert db.pending_calls == [
        {
            "limit": 1,
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
    assert db.profile_answer_updates == []
    assert any("[Search][Criteria]" in message and "ml engineer" in message for message in progress_messages)


@pytest.mark.asyncio
async def test_run_full_pipeline_uses_requested_max_jobs_for_current_run_apply_limit(monkeypatch):
    records = [make_job_record(job_id=i) for i in range(1, 5)]
    db = FakeDb(records)
    orch = Orchestrator(profile=make_profile(), db=db, runtime_scope="cli")
    applier = StubApplier()

    orch.run_scrape = AsyncMock(return_value=4)
    monkeypatch.setattr(orch, "_pick_applier", lambda job: applier)
    monkeypatch.setattr(settings, "dry_run", False)
    monkeypatch.setattr(settings, "max_applications_per_run", 2)

    counts = await orch.run_full_pipeline(
        JobSearchFilter(keywords=["ai engineer"], max_jobs=4),
        user_id=7,
        run_id="run-456",
        tailor_documents=False,
    )

    assert db.pending_calls == [
        {
            "limit": 4,
            "user_id": 7,
            "scrape_run_id": "run-456",
        }
    ]
    assert len(applier.calls) == 4
    assert counts["scraped"] == 4
    assert counts["applied"] == 4


@pytest.mark.asyncio
async def test_run_full_pipeline_scores_all_jobs_found_when_max_jobs_not_specified(monkeypatch):
    records = [make_job_record(job_id=i) for i in range(1, 25)]
    db = FakeDb(records)
    orch = Orchestrator(profile=make_profile(), db=db, runtime_scope="cli")
    progress_messages: list[str] = []

    orch.run_scrape = AsyncMock(return_value=24)
    monkeypatch.setattr(orch, "_get_ai_client", lambda: None)

    counts = await orch.run_full_pipeline(
        JobSearchFilter(keywords=["project manager"], min_match_score=75),
        user_id=11,
        run_id="run-score-all",
        progress_callback=progress_messages.append,
        tailor_documents=True,
    )

    assert db.pending_calls == [
        {
            "limit": 24,
            "user_id": 11,
            "scrape_run_id": "run-score-all",
        }
    ]
    assert counts["scraped"] == 24
    assert any("Scoring 24 jobs" in message for message in progress_messages)


@pytest.mark.asyncio
async def test_run_full_pipeline_emits_requested_vs_matched_message_when_fewer_jobs_found(monkeypatch):
    db = FakeDb([])
    orch = Orchestrator(profile=make_profile(), db=db)
    progress_messages: list[str] = []

    orch.run_scrape = AsyncMock(return_value=3)
    orch.run_apply = AsyncMock(
        return_value={"applied": 0, "failed": 0, "skipped": 0, "dry_run": 0}
    )

    counts = await orch.run_full_pipeline(
        JobSearchFilter(keywords=["product manager"], max_jobs=5),
        user_id=9,
        run_id="run-789",
        progress_callback=progress_messages.append,
        tailor_documents=False,
    )

    orch.run_apply.assert_awaited_once()
    assert orch.run_apply.await_args.kwargs["user_id"] == 9
    assert orch.run_apply.await_args.kwargs["scrape_run_id"] == "run-789"
    assert orch.run_apply.await_args.kwargs["limit"] == 5
    assert callable(orch.run_apply.await_args.kwargs["progress_callback"])
    assert counts["scraped"] == 3
    assert any(
        "Requested 5 jobs, but only 3 matched the current criteria. Proceeding with 3."
        in message
        for message in progress_messages
    )


@pytest.mark.asyncio
async def test_run_full_pipeline_does_not_emit_requested_vs_matched_message_when_target_is_met(monkeypatch):
    db = FakeDb([])
    orch = Orchestrator(profile=make_profile(), db=db)
    progress_messages: list[str] = []

    orch.run_scrape = AsyncMock(return_value=4)
    orch.run_apply = AsyncMock(
        return_value={"applied": 0, "failed": 0, "skipped": 0, "dry_run": 0}
    )

    await orch.run_full_pipeline(
        JobSearchFilter(keywords=["product manager"], max_jobs=4),
        user_id=9,
        run_id="run-790",
        progress_callback=progress_messages.append,
        tailor_documents=False,
    )

    assert not any("[Search][Result]" in message for message in progress_messages)


class LearningApplier:
    def __init__(self):
        self.progress_callback = None
        self.answer_resolver = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def apply(self, job, tailored_resume_path=None, cover_letter=None):
        assert self.answer_resolver is not None
        prompts = [
            ApplicationQuestionPrompt(
                question="Are you open to relocation?",
                field_type="radio",
                options=["Yes", "No"],
            )
        ]
        learned_answers = await self.answer_resolver(prompts)
        if self.progress_callback:
            self.progress_callback("[LinkedIn][Question][ai] Are you open to relocation? -> Yes")
        return ApplicationResult(
            job,
            ApplicationStatus.APPLIED,
            message="submitted",
            learned_answers=learned_answers,
        )


@pytest.mark.asyncio
async def test_run_apply_persists_learned_answers_from_applier(monkeypatch):
    record = make_job_record()
    db = FakeDb([record])
    orch = Orchestrator(profile=make_profile(), db=db, runtime_scope="cli")
    applier = LearningApplier()
    progress_messages: list[str] = []

    monkeypatch.setattr(orch, "_pick_applier", lambda job: applier)
    monkeypatch.setattr(orch, "_get_ai_client", lambda: object())
    monkeypatch.setattr(settings, "dry_run", False)

    with patch("src.orchestrator.profile_extractor.suggest_answers", new=AsyncMock(return_value={
        "Are you open to relocation?": "Yes"
    })), patch("src.orchestrator.save_profile") as save_profile_mock:
        counts = await orch.run_apply(user_id=42, progress_callback=progress_messages.append)

    assert counts["applied"] == 1
    assert db.profile_answer_updates == [
        {
            "user_id": 42,
            "custom_answers": {"Are you open to relocation?": "Yes"},
        }
    ]
    assert orch.profile.custom_answers["Are you open to relocation?"] == "Yes"
    assert any("[Profile][Saved]" in message for message in progress_messages)
    save_profile_mock.assert_called_once()


@pytest.mark.asyncio
async def test_run_apply_emits_applied_message_for_already_submitted_job(monkeypatch):
    record = make_job_record()
    db = FakeDb([record])
    orch = Orchestrator(profile=make_profile(), db=db)
    progress_messages: list[str] = []

    monkeypatch.setattr(orch, "_pick_applier", lambda job: AlreadySubmittedApplier())
    monkeypatch.setattr(settings, "dry_run", False)

    counts = await orch.run_apply(user_id=42, progress_callback=progress_messages.append)

    assert counts["applied"] == 1
    assert any(
        "applied — Application already submitted for this job" in message
        for message in progress_messages
    )


@pytest.mark.asyncio
async def test_run_apply_halts_remaining_linkedin_jobs_after_auth_failure(monkeypatch):
    records = [make_job_record(1), make_job_record(2)]
    db = FakeDb(records)
    orch = Orchestrator(profile=make_profile(), db=db)
    progress_messages: list[str] = []

    class FailingApplier:
        def __init__(self):
            self.calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def apply(self, job, tailored_resume_path=None, cover_letter=None):
            self.calls += 1
            return ApplicationResult(
                job,
                ApplicationStatus.FAILED,
                message="LinkedIn authentication unavailable: manual verification required.",
            )

    applier = FailingApplier()
    monkeypatch.setattr(orch, "_pick_applier", lambda job: applier)
    monkeypatch.setattr(settings, "dry_run", False)

    counts = await orch.run_apply(user_id=42, progress_callback=progress_messages.append)

    assert counts["failed"] == 2
    assert applier.calls == 1
    assert any("Halting remaining applications for this board" in message for message in progress_messages)
    assert db.status_updates[0]["notes"].startswith("LinkedIn authentication unavailable")
    assert db.status_updates[1]["notes"].startswith("LinkedIn authentication unavailable")


@pytest.mark.asyncio
async def test_handle_new_questions_preserves_prompt_metadata_and_normalizes_keys(monkeypatch):
    db = FakeDb([])
    orch = Orchestrator(profile=make_profile(), db=db, runtime_scope="cli")

    monkeypatch.setattr(orch, "_get_ai_client", lambda: object())

    prompts = [
        ApplicationQuestionPrompt(
            question=(
                "Are you comfortable commuting to this job's location?\n"
                "Are you comfortable commuting to this job's location?\nRequired"
            ),
            field_type="radio",
            options=["Yes", "No"],
        )
    ]
    suggest_mock = AsyncMock(
        return_value={"Are you comfortable commuting to this job's location?": "Yes"}
    )

    with patch("src.orchestrator.profile_extractor.suggest_answers", new=suggest_mock), patch(
        "src.orchestrator.save_profile"
    ) as save_profile_mock:
        await orch._handle_new_questions(prompts, user_id=42)

    called_prompts = suggest_mock.await_args.args[0]
    assert len(called_prompts) == 1
    assert called_prompts[0].question == "Are you comfortable commuting to this job's location?"
    assert called_prompts[0].field_type == "radio"
    assert called_prompts[0].options == ["Yes", "No"]
    assert db.profile_answer_updates == [
        {
            "user_id": 42,
            "custom_answers": {"Are you comfortable commuting to this job's location?": "Yes"},
        }
    ]
    assert orch.profile.custom_answers["Are you comfortable commuting to this job's location?"] == "Yes"
    save_profile_mock.assert_called_once()


class FakeScraper:
    def __init__(self, credentials=None, authenticated=True):
        self.credentials = credentials or {}
        self.detail_calls: list[str] = []
        self.authenticated = authenticated

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    def has_authenticated_session(self):
        return self.authenticated

    async def search(self, search_filter):
        for idx in range(3):
            yield Job(
                title=f"ML Engineer {idx}",
                company="Example Corp",
                url=f"https://www.linkedin.com/jobs/view/{idx}",
                source_board="linkedin",
                external_id=str(idx),
                easy_apply=False,
            )

    async def get_job_details(self, job):
        self.detail_calls.append(job.external_id or "")
        job.easy_apply = job.external_id == "0"
        job.description = "Verified by detail fetch"
        return job


@pytest.mark.asyncio
async def test_scrape_board_easy_apply_only_defers_filter_until_detail_fetch(monkeypatch):
    db = FakeDb([])
    orch = Orchestrator(profile=make_profile(), db=db)
    fake_scraper = FakeScraper(authenticated=True)

    monkeypatch.setitem(
        orchestrator_module.SCRAPER_REGISTRY,
        "linkedin",
        lambda credentials=None: fake_scraper,
    )
    monkeypatch.setattr(settings, "request_delay_seconds", 0)

    count = await orch._scrape_board(
        "linkedin",
        JobSearchFilter(
            keywords=["ml engineer"],
            easy_apply_only=True,
            max_age_days=0,
            max_jobs=1,
        ),
        user_id=9,
        run_id="run-xyz",
    )

    assert count == 1
    assert fake_scraper.detail_calls == ["0"]
    assert len(db.upserted_jobs) == 1
    assert db.upserted_jobs[0]["job"].external_id == "0"
    assert db.upserted_jobs[0]["job"].easy_apply is True
    assert db.upserted_jobs[0]["user_id"] == 9
    assert db.upserted_jobs[0]["scrape_run_id"] == "run-xyz"


class FalseEasyApplyScraper(FakeScraper):
    async def search(self, search_filter):
        yield Job(
            title="Platform Engineer",
            company="Example Corp",
            url="https://www.linkedin.com/jobs/view/10",
            source_board="linkedin",
            external_id="10",
            easy_apply=False,
        )

    async def get_job_details(self, job):
        self.detail_calls.append(job.external_id or "")
        job.easy_apply = False
        job.description = "Detail fetched"
        return job


@pytest.mark.asyncio
async def test_scrape_board_easy_apply_only_fails_closed_when_linkedin_session_is_degraded(monkeypatch):
    db = FakeDb([])
    orch = Orchestrator(profile=make_profile(), db=db)
    fake_scraper = FalseEasyApplyScraper(authenticated=False)

    monkeypatch.setitem(
        orchestrator_module.SCRAPER_REGISTRY,
        "linkedin",
        lambda credentials=None: fake_scraper,
    )
    monkeypatch.setattr(settings, "request_delay_seconds", 0)

    with pytest.raises(RuntimeError, match="LinkedIn authentication unavailable"):
        await orch._scrape_board(
            "linkedin",
            JobSearchFilter(
                keywords=["platform engineer"],
                easy_apply_only=True,
                max_age_days=0,
                max_jobs=1,
            ),
            user_id=9,
            run_id="run-keep",
        )

    assert fake_scraper.detail_calls == ["10"]
    assert db.upserted_jobs == []


@pytest.mark.asyncio
async def test_scrape_board_easy_apply_only_still_skips_non_easy_apply_when_authenticated(monkeypatch):
    db = FakeDb([])
    orch = Orchestrator(profile=make_profile(), db=db)
    fake_scraper = FalseEasyApplyScraper(authenticated=True)

    monkeypatch.setitem(
        orchestrator_module.SCRAPER_REGISTRY,
        "linkedin",
        lambda credentials=None: fake_scraper,
    )
    monkeypatch.setattr(settings, "request_delay_seconds", 0)

    count = await orch._scrape_board(
        "linkedin",
        JobSearchFilter(
            keywords=["platform engineer"],
            easy_apply_only=True,
            max_age_days=0,
            max_jobs=1,
        ),
        user_id=9,
        run_id="run-skip",
    )

    assert count == 0
    assert fake_scraper.detail_calls == ["10"]
    assert db.upserted_jobs == []


@pytest.mark.asyncio
async def test_scrape_board_skips_redundant_inter_job_delay_for_linkedin(monkeypatch):
    db = FakeDb([])
    orch = Orchestrator(profile=make_profile(), db=db)
    fake_scraper = FakeScraper(authenticated=True)
    sleep_mock = AsyncMock()

    monkeypatch.setitem(
        orchestrator_module.SCRAPER_REGISTRY,
        "linkedin",
        lambda credentials=None: fake_scraper,
    )
    monkeypatch.setattr(settings, "request_delay_seconds", 20)
    monkeypatch.setattr(orchestrator_module.asyncio, "sleep", sleep_mock)

    count = await orch._scrape_board(
        "linkedin",
        JobSearchFilter(
            keywords=["project manager"],
            easy_apply_only=False,
            max_age_days=0,
            max_jobs=2,
        ),
        user_id=9,
        run_id="run-fast",
    )

    assert count == 2
    sleep_mock.assert_not_awaited()
