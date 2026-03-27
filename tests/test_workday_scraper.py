from __future__ import annotations

from typing import Any

import httpx
import pytest

from src.models import JobSearchFilter, JobType, WorkMode
from src.scrapers.workday import WorkdayScraper


class DummyJsonResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code < 400:
            return
        request = httpx.Request("POST", "https://example.myworkdayjobs.com")
        response = httpx.Response(self.status_code, request=request)
        raise httpx.HTTPStatusError("request failed", request=request, response=response)


class RecordingAsyncClient:
    def __init__(self, *, post_responses: list[DummyJsonResponse], get_responses: list[DummyJsonResponse]):
        self._post_responses = post_responses
        self._get_responses = get_responses
        self.requests: list[dict[str, Any]] = []
        self.closed = False

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> DummyJsonResponse:
        self.requests.append({"method": "POST", "url": url, "json": dict(json or {}), "headers": dict(headers or {})})
        return self._post_responses.pop(0)

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> DummyJsonResponse:
        self.requests.append({"method": "GET", "url": url, "headers": dict(headers or {})})
        return self._get_responses.pop(0)

    async def aclose(self) -> None:
        self.closed = True


def test_parse_tenant_url_supports_common_workday_hosts() -> None:
    classic = WorkdayScraper._parse_tenant_url("https://acme.wd5.myworkdayjobs.com/en-US/Careers")
    assert classic.jobs_api_url == "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Careers/jobs"
    assert classic.detail_api_root == "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Careers"
    assert classic.company_name == "Acme"

    recruiting = WorkdayScraper._parse_tenant_url(
        "https://jobs.myworkdaysite.com/en-US/recruiting/contoso/External"
    )
    assert recruiting.jobs_api_url == "https://jobs.myworkdaysite.com/wday/cxs/contoso/External/jobs"
    assert recruiting.detail_api_root == "https://jobs.myworkdaysite.com/wday/cxs/contoso/External"
    assert recruiting.company_name == "Contoso"


@pytest.mark.asyncio
async def test_workday_search_uses_tenant_urls_filters_and_enriches_results() -> None:
    client = RecordingAsyncClient(
        post_responses=[
            DummyJsonResponse(
                {
                    "jobPostings": [
                        {
                            "title": "Senior Program Manager",
                            "externalPath": "/job/Ireland/Senior-Program-Manager_R-100",
                            "locationsText": "Remote - Ireland",
                            "postedOn": "Posted Today",
                            "bulletFields": ["Dublin", "Full time"],
                        },
                        {
                            "title": "Office Manager",
                            "externalPath": "/job/Ireland/Office-Manager_R-200",
                            "locationsText": "Dublin, Ireland",
                            "postedOn": "Posted 12 Days Ago",
                            "bulletFields": ["On-site"],
                        },
                    ]
                }
            )
        ],
        get_responses=[
            DummyJsonResponse(
                {
                    "title": "Senior Program Manager",
                    "location": "Remote - Ireland",
                    "postedOn": "Posted Today",
                    "timeType": "Full time",
                    "jobPostingInfo": {
                        "jobDescription": "<p>Lead transformation delivery across product and engineering.</p>"
                    },
                }
            )
        ],
    )
    scraper = WorkdayScraper(credentials={"tenant_urls": ["https://acme.wd5.myworkdayjobs.com/en-US/Careers"]})
    scraper._client = client

    async with scraper:
        jobs = [
            job
            async for job in scraper.search(
                JobSearchFilter(
                    keywords=["program", "manager"],
                    remote_only=True,
                    job_types=[JobType.FULL_TIME],
                    max_age_days=7,
                )
            )
        ]

    assert client.closed is True
    assert len(jobs) == 1
    assert jobs[0].title == "Senior Program Manager"
    assert jobs[0].company == "Acme"
    assert jobs[0].job_type == JobType.FULL_TIME
    assert jobs[0].work_mode == WorkMode.REMOTE
    assert jobs[0].description == "Lead transformation delivery across product and engineering."
    assert jobs[0].external_id == "Senior-Program-Manager_R-100"
    assert jobs[0].url == "https://acme.wd5.myworkdayjobs.com/job/Ireland/Senior-Program-Manager_R-100"

    assert client.requests[0] == {
        "method": "POST",
        "url": "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Careers/jobs",
        "json": {
            "appliedFacets": {},
            "limit": 20,
            "offset": 0,
            "searchText": "program manager",
        },
        "headers": {"Referer": "https://acme.wd5.myworkdayjobs.com/en-US/Careers"},
    }
    assert client.requests[1]["method"] == "GET"
    assert client.requests[1]["url"] == (
        "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Careers/job/Ireland/Senior-Program-Manager_R-100"
    )
