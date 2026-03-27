from __future__ import annotations

from typing import AsyncIterator

from loguru import logger

from src.models import Job, JobSearchFilter
from src.scrapers.base import BaseScraper


class IndeedScraper(BaseScraper):
    """Scrapes job listings from Indeed.

    Strategy:
    - Guest HTTP scraping of indeed.com/jobs search pages.
    - Uses rotating user-agents + optional proxy support to avoid rate limits.
    - Playwright fallback for JS-rendered pages.
    """

    board_name = "Indeed"
    board_slug = "indeed"
    requires_auth = False

    BASE_URL = "https://www.indeed.com"
    JOBS_SEARCH_URL = "https://www.indeed.com/jobs"

    async def setup(self) -> None:
        await super().setup()
        # TODO: initialise httpx.AsyncClient with rotating headers

    async def teardown(self) -> None:
        # TODO: close client
        await super().teardown()

    async def search(self, search_filter: JobSearchFilter) -> AsyncIterator[Job]:
        logger.info(f"[Indeed] Starting search: {search_filter.keywords}")
        # TODO:
        # 1. Build query params: q=keywords, l=location, fromage=max_age_days
        # 2. Paginate through result pages
        # 3. Parse job cards (title, company, location, job key / URL)
        # 4. yield self._make_job(...)
        raise NotImplementedError("Indeed scraper not yet implemented")
        yield

    async def get_job_details(self, job: Job) -> Job:
        # TODO: fetch viewjob page, parse full description
        raise NotImplementedError
        return job
