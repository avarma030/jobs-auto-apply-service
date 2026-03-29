"""Unit tests for the LinkedIn scraper.

Tests focus on HTML parsing logic and parameter building — no real HTTP calls.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.models import ExperienceLevel, JobSearchFilter, JobType, WorkMode
from src.scrapers.linkedin import LinkedInScraper


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_card_html(job_id: str, title: str, *, easy_apply: bool = False) -> str:
    easy_apply_markup = (
        '<span class="result-benefits__text">Easy Apply</span>'
        if easy_apply
        else ""
    )
    return f"""
<ul>
  <li>
    <div class="base-card"
         data-entity-urn="urn:li:jobPosting:{job_id}">
      <a class="base-card__full-link"
         href="https://www.linkedin.com/jobs/view/{job_id}/?refId=abc">
      </a>
      <div class="base-search-card__info">
        <h3 class="base-search-card__title">{title}</h3>
        <h4 class="base-search-card__subtitle">
          <a class="hidden-nested-link" href="/company/acme">Acme Corp</a>
        </h4>
        <div class="base-search-card__metadata">
          <span class="job-search-card__location">San Francisco, CA (Remote)</span>
          <time datetime="2024-06-01T00:00:00.000Z">3 days ago</time>
        </div>
        {easy_apply_markup}
      </div>
    </div>
  </li>
</ul>
"""


SAMPLE_CARD_HTML = make_card_html("3987654321", "Senior Python Engineer")
SAMPLE_EASY_APPLY_CARD_HTML = make_card_html(
    "3987654322",
    "Senior Python Engineer II",
    easy_apply=True,
)

SAMPLE_DETAIL_HTML = """
<html>
<body>
  <div class="show-more-less-html__markup">
    <p>We are looking for a Senior Python Engineer.</p>
    <p>Requirements: Python, Django, PostgreSQL</p>
  </div>
  <button class="jobs-apply-button" aria-label="Easy Apply to Senior Python Engineer">
    Easy Apply
  </button>
  <ul>
    <li class="description__job-criteria-item">
      <h3 class="description__job-criteria-subheader">Employment type</h3>
      <span class="description__job-criteria-text">Full-time</span>
    </li>
    <li class="description__job-criteria-item">
      <h3 class="description__job-criteria-subheader">Seniority level</h3>
      <span class="description__job-criteria-text">Senior</span>
    </li>
  </ul>
</body>
</html>
"""


# ── Scraper setup helpers ──────────────────────────────────────────────────────

def make_scraper() -> LinkedInScraper:
    from fake_useragent import UserAgent
    scraper = LinkedInScraper.__new__(LinkedInScraper)
    scraper.credentials = {}
    scraper._session_active = False
    scraper._ua = UserAgent()
    scraper._proxies = []
    scraper._proxy_index = 0
    scraper._cookies = {}
    scraper._warm_attempted = False
    scraper._detail_api_authwall_backoff_until = 0.0
    return scraper


def test_ensure_linkedin_state_scopes_paths_to_credentials():
    scraper = make_scraper()
    scraper.credentials = {
        "username": "rucha@example.com",
        "password": "secret",
    }

    scraper._ensure_linkedin_state()

    cookie_path = str(scraper._cookie_path).replace("\\", "/")
    session_dir = str(scraper._session_dir).replace("\\", "/")
    detail_dir = str(scraper._detail_session_dir).replace("\\", "/")
    assert "data/linkedin/" in cookie_path
    assert "rucha-example-com" in cookie_path
    assert cookie_path.endswith("/cookies.json")
    assert session_dir.endswith("/scraper_session")
    assert detail_dir.endswith("/detail_session")


def test_load_cookies_ignores_different_account_owner(tmp_path):
    scraper = make_scraper()
    scraper.credentials = {
        "username": "rucha@example.com",
        "password": "secret",
    }
    scraper._ensure_linkedin_state()
    scraper._cookie_path = tmp_path / "cookies.json"
    scraper._cookie_path.write_text(
        json.dumps(
            {
                "saved_at": time.time(),
                "username": "akshay@example.com",
                "cookies": {"li_at": "old-cookie"},
            }
        ),
        encoding="utf-8",
    )

    with patch(
        "src.scrapers.linkedin.legacy_linkedin_cookie_path",
        return_value=tmp_path / "missing_legacy.json",
    ):
        assert scraper._load_cookies() is None


def test_load_cookies_falls_back_to_legacy_authenticated_cookie_file(tmp_path):
    scraper = make_scraper()
    scraper.credentials = {
        "username": "rucha@example.com",
        "password": "secret",
    }
    scraper._ensure_linkedin_state()
    scraper._cookie_path = tmp_path / "scoped_cookies.json"
    scraper._cookie_path.write_text(
        json.dumps(
            {
                "saved_at": time.time(),
                "username": "rucha@example.com",
                "cookies": {"lang": "en-us"},
            }
        ),
        encoding="utf-8",
    )
    legacy_cookie_path = tmp_path / "legacy_cookies.json"
    legacy_cookie_path.write_text(
        json.dumps(
            {
                "saved_at": time.time(),
                "username": "rucha@example.com",
                "cookies": {"li_at": "legacy-li-at", "JSESSIONID": '"ajax:123"'},
            }
        ),
        encoding="utf-8",
    )

    with patch("src.scrapers.linkedin.legacy_linkedin_cookie_path", return_value=legacy_cookie_path):
        cookies = scraper._load_cookies()

    assert cookies is not None
    assert cookies["li_at"] == "legacy-li-at"


def test_load_cookies_refuses_legacy_authenticated_cookie_file_for_other_account(tmp_path):
    scraper = make_scraper()
    scraper.credentials = {
        "username": "rucha@example.com",
        "password": "secret",
    }
    scraper._ensure_linkedin_state()
    scraper._cookie_path = tmp_path / "scoped_cookies.json"
    scraper._cookie_path.write_text(
        json.dumps(
            {
                "saved_at": time.time(),
                "username": "rucha@example.com",
                "cookies": {"lang": "en-us"},
            }
        ),
        encoding="utf-8",
    )
    legacy_cookie_path = tmp_path / "legacy_cookies.json"
    legacy_cookie_path.write_text(
        json.dumps(
            {
                "saved_at": time.time(),
                "username": "akshay@example.com",
                "cookies": {"li_at": "legacy-li-at", "JSESSIONID": '"ajax:123"'},
            }
        ),
        encoding="utf-8",
    )

    with patch("src.scrapers.linkedin.legacy_linkedin_cookie_path", return_value=legacy_cookie_path):
        cookies = scraper._load_cookies()

    assert cookies is None


# ── Tests: _parse_job_cards ───────────────────────────────────────────────────

class TestParseJobCards:
    def test_parses_job_id(self):
        scraper = make_scraper()
        cards = scraper._parse_job_cards(SAMPLE_CARD_HTML)
        assert len(cards) == 1
        assert cards[0]["job_id"] == "3987654321"

    def test_parses_title(self):
        scraper = make_scraper()
        cards = scraper._parse_job_cards(SAMPLE_CARD_HTML)
        assert cards[0]["title"] == "Senior Python Engineer"

    def test_parses_company(self):
        scraper = make_scraper()
        cards = scraper._parse_job_cards(SAMPLE_CARD_HTML)
        assert cards[0]["company"] == "Acme Corp"

    def test_parses_location(self):
        scraper = make_scraper()
        cards = scraper._parse_job_cards(SAMPLE_CARD_HTML)
        assert "San Francisco" in cards[0]["location"]

    def test_detects_remote_work_mode(self):
        scraper = make_scraper()
        cards = scraper._parse_job_cards(SAMPLE_CARD_HTML)
        assert cards[0]["work_mode"] == WorkMode.REMOTE

    def test_parses_url_without_query_string(self):
        scraper = make_scraper()
        cards = scraper._parse_job_cards(SAMPLE_CARD_HTML)
        assert "?" not in cards[0]["url"]
        assert "3987654321" in cards[0]["url"]

    def test_parses_posted_at_datetime(self):
        scraper = make_scraper()
        cards = scraper._parse_job_cards(SAMPLE_CARD_HTML)
        assert isinstance(cards[0]["posted_at"], datetime)
        assert cards[0]["posted_at"].year == 2024

    def test_empty_html_returns_empty_list(self):
        scraper = make_scraper()
        assert scraper._parse_job_cards("") == []
        assert scraper._parse_job_cards("<ul></ul>") == []

    def test_detects_easy_apply_from_footer_text(self):
        scraper = make_scraper()
        footer_html = """
<ul>
  <li>
    <div class="base-card" data-entity-urn="urn:li:jobPosting:3987654333">
      <a class="base-card__full-link"
         href="https://www.linkedin.com/jobs/view/3987654333/?refId=abc">
      </a>
      <div class="base-search-card__info">
        <h3 class="base-search-card__title">Platform Engineer</h3>
        <h4 class="base-search-card__subtitle">
          <a class="hidden-nested-link" href="/company/acme">Acme Corp</a>
        </h4>
        <div class="base-search-card__metadata">
          <span class="job-search-card__location">Remote</span>
          <span class="job-search-card__footer-item">Easy Apply</span>
          <time datetime="2024-06-01T00:00:00.000Z">3 days ago</time>
        </div>
      </div>
    </div>
  </li>
</ul>
"""
        cards = scraper._parse_job_cards(footer_html)
        assert cards[0]["easy_apply"] is True


# ── Tests: _build_search_params ───────────────────────────────────────────────

class TestBuildSearchParams:
    def test_basic_keywords(self):
        scraper = make_scraper()
        f = JobSearchFilter(keywords=["python", "django"])
        params = scraper._build_search_params(f)
        assert params["keywords"] == "python django"

    def test_remote_only_sets_f_wt(self):
        scraper = make_scraper()
        f = JobSearchFilter(keywords=["engineer"], remote_only=True)
        params = scraper._build_search_params(f)
        assert params["f_WT"] == "2"

    def test_location_included(self):
        scraper = make_scraper()
        f = JobSearchFilter(keywords=["engineer"], location="New York")
        params = scraper._build_search_params(f)
        assert params["location"] == "New York"

    def test_max_age_days_sets_f_tpr(self):
        scraper = make_scraper()
        f = JobSearchFilter(keywords=["engineer"], max_age_days=7)
        params = scraper._build_search_params(f)
        assert params["f_TPR"] == "r604800"

    def test_job_type_full_time(self):
        scraper = make_scraper()
        f = JobSearchFilter(keywords=["engineer"], job_types=[JobType.FULL_TIME])
        params = scraper._build_search_params(f)
        assert "F" in params.get("f_JT", "")

    def test_experience_level_senior(self):
        scraper = make_scraper()
        f = JobSearchFilter(keywords=["engineer"], experience_levels=[ExperienceLevel.SENIOR])
        params = scraper._build_search_params(f)
        assert "4" in params.get("f_E", "")


# ── Tests: _parse_salary ──────────────────────────────────────────────────────

class TestParseSalary:
    def test_range_with_k_suffix(self):
        scraper = make_scraper()
        low, high, cur = scraper._parse_salary("$120K/yr – $160K/yr")
        assert low == 120_000
        assert high == 160_000
        assert cur == "USD"

    def test_single_value(self):
        scraper = make_scraper()
        low, high, cur = scraper._parse_salary("$95,000/yr")
        assert low == 95_000
        assert high is None

    def test_gbp_currency(self):
        scraper = make_scraper()
        _, _, cur = scraper._parse_salary("£50K – £70K")
        assert cur == "GBP"

    def test_no_salary_info(self):
        scraper = make_scraper()
        low, high, cur = scraper._parse_salary("Competitive")
        assert low is None
        assert high is None


# ── Tests: _parse_job_type ────────────────────────────────────────────────────

class TestParseJobType:
    def test_full_time(self):
        assert LinkedInScraper._parse_job_type("Full-time") == JobType.FULL_TIME

    def test_contract(self):
        assert LinkedInScraper._parse_job_type("Contract") == JobType.CONTRACT

    def test_internship(self):
        assert LinkedInScraper._parse_job_type("Internship") == JobType.INTERNSHIP

    def test_unknown_returns_none(self):
        assert LinkedInScraper._parse_job_type("Other") is None


# ── Tests: _parse_experience_level ───────────────────────────────────────────

class TestParseExperienceLevel:
    def test_senior(self):
        assert LinkedInScraper._parse_experience_level("Senior") == ExperienceLevel.SENIOR

    def test_entry(self):
        assert LinkedInScraper._parse_experience_level("Entry level") == ExperienceLevel.ENTRY

    def test_director(self):
        assert LinkedInScraper._parse_experience_level("Director") == ExperienceLevel.EXECUTIVE

    def test_unknown_returns_none(self):
        assert LinkedInScraper._parse_experience_level("Unknown band") is None


# ── Tests: get_job_details ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_job_details_enriches_job():
    from src.models import Job

    scraper = make_scraper()
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=MagicMock(
        text=SAMPLE_DETAIL_HTML,
        status_code=200,
        raise_for_status=MagicMock(),
    ))
    mock_client.headers = {}
    scraper._client = mock_client

    job = Job(
        title="Senior Python Engineer",
        company="Acme Corp",
        url="https://www.linkedin.com/jobs/view/3987654321/",
        source_board="linkedin",
        external_id="3987654321",
    )

    enriched = await scraper.get_job_details(job)

    assert enriched.easy_apply is True
    assert enriched.description is not None
    assert "Python" in enriched.description
    assert enriched.job_type == JobType.FULL_TIME
    assert enriched.experience_level == ExperienceLevel.SENIOR


# ── Tests: search pagination ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_yields_jobs_and_stops_on_empty():
    scraper = make_scraper()

    call_count = 0

    async def fake_fetch(params):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return SAMPLE_CARD_HTML
        return ""  # empty page → stop

    scraper._fetch_search_page = fake_fetch

    # max_age_days=0 disables the date cutoff so the 2024 sample card isn't rejected
    f = JobSearchFilter(keywords=["python"], max_age_days=0)
    jobs = []
    async for job in scraper.search(f):
        jobs.append(job)

    assert len(jobs) == 1
    assert jobs[0].title == "Senior Python Engineer"
    assert jobs[0].source_board == "linkedin"
    assert call_count == 2  # fetched once with results, once empty


@pytest.mark.asyncio
async def test_search_deduplicates_same_job_id():
    scraper = make_scraper()
    call_count = 0

    async def fake_fetch(params):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return SAMPLE_CARD_HTML  # same card twice
        return ""

    scraper._fetch_search_page = fake_fetch

    # max_age_days=0 disables the date cutoff so the 2024 sample card isn't rejected
    f = JobSearchFilter(keywords=["python"], max_age_days=0)
    jobs = []
    async for job in scraper.search(f):
        jobs.append(job)

    # Even though the card appeared twice, only one job should be yielded
    assert len(jobs) == 1


@pytest.mark.asyncio
async def test_search_easy_apply_only_defers_filter_until_detail_fetch():
    scraper = make_scraper()
    call_count = 0

    async def fake_fetch(params):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return SAMPLE_CARD_HTML
        if call_count == 2:
            return SAMPLE_EASY_APPLY_CARD_HTML
        return ""

    scraper._fetch_search_page = fake_fetch

    f = JobSearchFilter(keywords=["python"], max_age_days=0, easy_apply_only=True)
    jobs = []
    async for job in scraper.search(f):
        jobs.append(job)

    assert len(jobs) == 2
    assert jobs[0].external_id == "3987654321"
    assert jobs[0].easy_apply is False
    assert jobs[1].external_id == "3987654322"
    assert jobs[1].easy_apply is True
    assert call_count == 3


@pytest.mark.asyncio
async def test_guest_detail_authwall_enters_backoff_and_falls_back_to_browser():
    scraper = make_scraper()
    scraper._rewarm = AsyncMock()
    scraper._client = MagicMock()
    scraper._client.headers = {}
    scraper._client.get = AsyncMock(side_effect=[
        MagicMock(text="authwall", status_code=200, raise_for_status=MagicMock()),
        MagicMock(text="authwall", status_code=200, raise_for_status=MagicMock()),
    ])

    url = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/3987654321"

    with patch(
        "src.scrapers.linkedin.time.time",
        side_effect=[100.0, 101.0, 102.0, 150.0, 150.0],
    ):
        html = await scraper._get(url)
        second_html = await scraper._get(url)

    assert html == ""
    assert second_html == ""
    assert scraper._rewarm.await_count == 1
    assert scraper._client.get.await_count == 2
    assert scraper._detail_api_authwall_backoff_until == 402.0
