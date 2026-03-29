"""Unit tests for the LinkedIn applier.

Browser interactions are fully mocked — no real Playwright needed.
"""
from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.appliers.linkedin as linkedin_mod
from src.appliers.base import ApplicationResult
from src.appliers.linkedin import LinkedInApplier
from src.models import ApplicationStatus, Job, UserProfile
from src.models.user_profile import (
    Address,
    ApplicationPreferences,
    JobBoardAccounts,
    JobBoardCredentials,
    SocialLinks,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_profile(**overrides) -> UserProfile:
    base = dict(
        first_name="Jane",
        last_name="Doe",
        email="jane@example.com",
        phone="+1-555-123-4567",
        address=Address(city="San Francisco", state="CA", zip_code="94105", country="US"),
        headline="Senior Software Engineer",
        summary="Experienced engineer.",
        years_of_experience=7,
        skills=["Python", "Django", "PostgreSQL"],
        social_links=SocialLinks(
            linkedin="https://linkedin.com/in/janedoe",
            github="https://github.com/janedoe",
        ),
        job_board_accounts=JobBoardAccounts(
            linkedin=JobBoardCredentials(
                username="jane@example.com", password="secret"
            )
        ),
        preferences=ApplicationPreferences(),
        custom_answers={
            "Are you legally authorized to work in the US?": "Yes",
            "Will you require sponsorship?": "No",
            "Years of experience with Python": "7",
        },
    )
    base.update(overrides)
    return UserProfile(**base)


def make_job(easy_apply: bool = True, **overrides) -> Job:
    base = dict(
        title="Senior Python Engineer",
        company="Acme Corp",
        url="https://www.linkedin.com/jobs/view/1234567890/",
        source_board="linkedin",
        external_id="1234567890",
        easy_apply=easy_apply,
    )
    base.update(overrides)
    return Job(**base)


def make_applier(profile: UserProfile | None = None) -> LinkedInApplier:
    applier = LinkedInApplier.__new__(LinkedInApplier)
    applier.profile = profile or make_profile()
    applier._bm = None
    applier._page = None
    applier._logged_in = False
    return applier


# ── Tests: can_apply ──────────────────────────────────────────────────────────

class TestCanApply:
    def test_linkedin_job(self):
        applier = make_applier()
        job = make_job(source_board="linkedin")
        assert applier.can_apply(job) is True

    def test_non_linkedin_job(self):
        applier = make_applier()
        job = make_job(source_board="indeed")
        assert applier.can_apply(job) is False


# ── Tests: apply routing ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_apply_skips_non_linkedin_job():
    applier = make_applier()
    job = make_job(source_board="indeed")
    result = await applier.apply(job)
    assert result.status == ApplicationStatus.SKIPPED


@pytest.mark.asyncio
async def test_apply_ignores_stale_easy_apply_flag_and_checks_live_page():
    applier = make_applier()
    applier._page = MagicMock()
    applier._page.url = "https://www.linkedin.com/jobs/"
    applier._logged_in = True
    applier._is_logged_in = AsyncMock(return_value=True)
    job = make_job(easy_apply=False)
    expected = ApplicationResult(job, ApplicationStatus.SKIPPED, "checked live page")
    applier._easy_apply = AsyncMock(return_value=expected)
    result = await applier.apply(job)
    assert result is expected
    applier._easy_apply.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_fails_if_browser_not_initialised():
    applier = make_applier()
    applier._page = None
    job = make_job()
    result = await applier.apply(job)
    assert result.status == ApplicationStatus.FAILED
    assert "Browser not initialised" in result.message


@pytest.mark.asyncio
async def test_inject_scraper_cookies_keeps_jsessionid_js_readable(tmp_path):
    cookie_path = tmp_path / "linkedin_cookies.json"
    cookie_path.write_text(
        json.dumps(
            {
                "saved_at": time.time(),
                "cookies": {
                    "li_at": "li-at-token",
                    "JSESSIONID": '"ajax:123"',
                    "lang": "v=2&lang=en-us",
                },
            }
        ),
        encoding="utf-8",
    )

    applier = make_applier()
    applier._bm = MagicMock()
    applier._bm._context = MagicMock()
    applier._bm._context.add_cookies = AsyncMock()

    with patch.object(linkedin_mod, "_COOKIE_PATH", cookie_path):
        injected = await applier._inject_scraper_cookies()

    assert injected is True
    applier._bm._context.add_cookies.assert_awaited_once()
    cookies = applier._bm._context.add_cookies.await_args.args[0]
    by_name = {cookie["name"]: cookie for cookie in cookies}
    assert by_name["li_at"]["httpOnly"] is True
    assert by_name["JSESSIONID"]["httpOnly"] is False


@pytest.mark.asyncio
async def test_is_logged_in_rejects_authwall_job_pages():
    applier = make_applier()
    page = MagicMock()
    page.url = "https://www.linkedin.com/authwall?sessionRedirect=https%3A%2F%2Fwww.linkedin.com%2Fjobs%2Fview%2F123"
    empty_locator = MagicMock()
    empty_locator.count = AsyncMock(return_value=0)
    page.locator.return_value = empty_locator

    assert await applier._is_logged_in(page) is False


def test_modal_descendants_scopes_each_modal_root():
    scoped = LinkedInApplier._modal_descendants("input[type='text']")
    assert scoped == (
        "div.jobs-easy-apply-modal input[type='text'], "
        "div.jobs-apply-modal input[type='text'], "
        "div[role='dialog'] input[type='text']"
    )


# ── Tests: _infer_value_from_label ────────────────────────────────────────────

class TestInferValueFromLabel:
    def test_first_name(self):
        applier = make_applier()
        assert applier._infer_value_from_label("First name") == "Jane"

    def test_last_name(self):
        applier = make_applier()
        assert applier._infer_value_from_label("Last Name") == "Doe"

    def test_email(self):
        applier = make_applier()
        assert applier._infer_value_from_label("Email address") == "jane@example.com"

    def test_phone(self):
        applier = make_applier()
        assert applier._infer_value_from_label("Phone number") == "+1-555-123-4567"

    def test_linkedin_url(self):
        applier = make_applier()
        assert applier._infer_value_from_label("LinkedIn profile") == "https://linkedin.com/in/janedoe"

    def test_city(self):
        applier = make_applier()
        assert applier._infer_value_from_label("City") == "San Francisco"

    def test_years_experience(self):
        applier = make_applier()
        assert applier._infer_value_from_label("Years of experience") == "7"

    def test_unknown_label_returns_none(self):
        applier = make_applier()
        assert applier._infer_value_from_label("Favourite colour") is None


# ── Tests: _answer_for_label ─────────────────────────────────────────────────

class TestAnswerForLabel:
    def test_exact_key_match(self):
        applier = make_applier()
        result = applier._answer_for_label("Are you legally authorized to work in the US?")
        assert result == "Yes"

    def test_partial_match(self):
        applier = make_applier()
        result = applier._answer_for_label("Will you require sponsorship?")
        assert result == "No"

    def test_no_match_returns_none(self):
        applier = make_applier()
        result = applier._answer_for_label("Favourite ice cream flavour")
        assert result is None


# ── Tests: _build_cover_letter ───────────────────────────────────────────────

class TestBuildCoverLetter:
    def test_includes_job_title_and_company(self):
        applier = make_applier()
        job = make_job()
        cl = applier._build_cover_letter(job)
        assert "Senior Python Engineer" in cl
        assert "Acme Corp" in cl

    def test_includes_applicant_name(self):
        applier = make_applier()
        job = make_job()
        cl = applier._build_cover_letter(job)
        assert "Jane" in cl
        assert "Doe" in cl

    def test_uses_template_if_path_provided(self, tmp_path):
        template = tmp_path / "cover_letter.txt"
        template.write_text(
            "Dear Hiring Team,\n\nI want to join {company} as {job_title}.\n\n{first_name} {last_name}"
        )
        profile = make_profile(cover_letter_template_path=template)
        applier = make_applier(profile)
        job = make_job()
        cl = applier._build_cover_letter(job)
        assert "Acme Corp" in cl
        assert "Senior Python Engineer" in cl
        assert "Jane Doe" in cl
