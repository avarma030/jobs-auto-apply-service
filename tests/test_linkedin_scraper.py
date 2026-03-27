from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock

import httpx
import pytest

from src.models import ExperienceLevel, JobSearchFilter, JobType
from src.scrapers.linkedin import LinkedInScraper


class DummyResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code < 400:
            return
        request = httpx.Request("GET", "https://www.linkedin.com")
        response = httpx.Response(self.status_code, request=request)
        raise httpx.HTTPStatusError("request failed", request=request, response=response)


class RecordingAsyncClient:
    def __init__(self, responses: list[DummyResponse]):
        self._responses = responses
        self.requests: list[dict[str, object]] = []
        self.closed = False

    async def get(self, url: str, *, params: dict[str, object] | None = None) -> DummyResponse:
        self.requests.append({"url": url, "params": dict(params or {})})
        return self._responses.pop(0)

    async def aclose(self) -> None:
        self.closed = True


def render_job_card(
    job_id: str,
    *,
    title: str = "Python Engineer",
    company: str = "Acme",
    location: str = "Remote",
    posted_at: datetime | None = None,
    easy_apply: bool = False,
) -> str:
    time_markup = ""
    if posted_at is not None:
        iso_date = posted_at.strftime("%Y-%m-%d")
        time_markup = f'<time datetime="{iso_date}">{iso_date}</time>'

    easy_apply_markup = ""
    if easy_apply:
        easy_apply_markup = '<span class="job-search-card__easy-apply-label">Easy Apply</span>'

    return f"""
    <li>
      <div class="base-search-card" data-entity-urn="urn:li:jobPosting:{job_id}">
        <h3 class="base-search-card__title">{title}</h3>
        <h4 class="base-search-card__subtitle">{company}</h4>
        <span class="job-search-card__location">{location}</span>
        <a class="base-card__full-link" href="/jobs/view/{job_id}/"></a>
        {time_markup}
        {easy_apply_markup}
      </div>
    </li>
    """


def render_job_detail(
    *,
    description: str = "Build APIs and data pipelines.",
    seniority: str = "Mid-Senior level",
    employment_type: str = "Full-time",
    job_function: str = "Engineering",
    industries: str = "Software Development",
    apply_label: str = "Easy Apply",
) -> str:
    return f"""
    <html>
      <body>
        <div class="top-card-layout">
          <a class="top-card-layout__cta">{apply_label}</a>
        </div>
        <span class="posted-time-ago__text">3 days ago</span>
        <div class="show-more-less-html__markup">{description}</div>
        <ul class="description__job-criteria-list">
          <li class="description__job-criteria-item">
            <h3 class="description__job-criteria-subheader">Seniority level</h3>
            <span class="description__job-criteria-text">{seniority}</span>
          </li>
          <li class="description__job-criteria-item">
            <h3 class="description__job-criteria-subheader">Employment type</h3>
            <span class="description__job-criteria-text">{employment_type}</span>
          </li>
          <li class="description__job-criteria-item">
            <h3 class="description__job-criteria-subheader">Job function</h3>
            <span class="description__job-criteria-text">{job_function}</span>
          </li>
          <li class="description__job-criteria-item">
            <h3 class="description__job-criteria-subheader">Industries</h3>
            <span class="description__job-criteria-text">{industries}</span>
          </li>
        </ul>
      </body>
    </html>
    """


@pytest.mark.asyncio
async def test_linkedin_search_uses_api_filters_and_parses_results() -> None:
    html = f"""
    <ul>
      {render_job_card("101", company="Acme", posted_at=datetime(2026, 3, 25))}
      {render_job_card("202", title="Backend Engineer", company="Globex", location="Dublin, Ireland")}
    </ul>
    """
    client = RecordingAsyncClient(
        [
            DummyResponse(html),
            DummyResponse(
                render_job_detail(
                    description="Build APIs and data pipelines.",
                    seniority="Mid-Senior level",
                    employment_type="Full-time",
                    job_function="Engineering",
                    industries="Software Development",
                    apply_label="Easy Apply",
                )
            ),
            DummyResponse(
                render_job_detail(
                    description="Own backend services.",
                    seniority="Entry level",
                    employment_type="Contract",
                    job_function="Engineering",
                    industries="Financial Services",
                    apply_label="Apply",
                )
            ),
        ]
    )
    scraper = LinkedInScraper()
    scraper._client = client

    search_filter = JobSearchFilter(
        keywords=["python", "engineer"],
        location="Dublin, Ireland",
        remote_only=True,
        job_types=[JobType.FULL_TIME, JobType.CONTRACT],
        experience_levels=[ExperienceLevel.ENTRY, ExperienceLevel.MID],
        max_age_days=7,
    )

    async with scraper:
        jobs = [job async for job in scraper.search(search_filter)]

    assert client.closed is True
    assert len(client.requests) == 3
    assert client.requests[0]["url"] == scraper.JOBS_API_URL
    assert client.requests[0]["params"] == {
        "start": 0,
        "keywords": "python engineer",
        "location": "Dublin, Ireland",
        "f_WT": "2",
        "f_JT": "F,C",
        "f_E": "2,3",
        "f_TPR": "r604800",
    }
    assert client.requests[1]["url"] == "https://www.linkedin.com/jobs/view/101/"
    assert client.requests[2]["url"] == "https://www.linkedin.com/jobs/view/202/"

    assert len(jobs) == 2
    assert jobs[0].source_board == "linkedin"
    assert jobs[0].external_id == "101"
    assert jobs[0].url == "https://www.linkedin.com/jobs/view/101/"
    assert jobs[0].easy_apply is True
    assert jobs[0].posted_at == datetime(2026, 3, 25)
    assert jobs[0].description == "Build APIs and data pipelines."
    assert jobs[0].job_type == "full_time"
    assert jobs[0].experience_level == "senior"
    assert jobs[0].tags == ["Engineering", "Software Development"]
    assert jobs[1].title == "Backend Engineer"
    assert jobs[1].company == "Globex"
    assert jobs[1].description == "Own backend services."
    assert jobs[1].job_type == "contract"
    assert jobs[1].experience_level == "entry"
    assert jobs[1].easy_apply is False


@pytest.mark.asyncio
async def test_linkedin_search_paginates_and_deduplicates_across_pages() -> None:
    page_one = "<ul>" + "".join(render_job_card(str(index)) for index in range(1, 26)) + "</ul>"
    page_two = (
        "<ul>"
        + render_job_card("5", title="Duplicate")
        + render_job_card("26", title="Newest Role", company="Initech")
        + "</ul>"
    )
    client = RecordingAsyncClient([DummyResponse(page_one), DummyResponse(page_two)])
    scraper = LinkedInScraper()
    scraper._client = client
    scraper.get_job_details = AsyncMock(side_effect=lambda job: job)

    async with scraper:
        jobs = [job async for job in scraper.search(JobSearchFilter(keywords=["python"]))]

    assert [request["params"] for request in client.requests] == [
        {"start": 0, "keywords": "python", "f_TPR": "r604800"},
        {"start": 25, "keywords": "python", "f_TPR": "r604800"},
    ]
    assert len(jobs) == 26
    assert jobs[-1].external_id == "26"
    assert jobs[-1].title == "Newest Role"
