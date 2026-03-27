from __future__ import annotations

from typing import AsyncIterator

from loguru import logger

from src.models import Job, JobSearchFilter
from src.scrapers.base import BaseScraper


class GlassdoorScraper(BaseScraper):
    """Scrapes job listings from Glassdoor.

    Strategy:
    - Glassdoor requires a login for most job details; browser automation
      via Playwright with stored session cookies is the primary approach.
    - Salary data is a key differentiator available here.
    """

    board_name = "Glassdoor"
    board_slug = "glassdoor"
    requires_auth = True

    BASE_URL = "https://www.glassdoor.com"
    JOBS_SEARCH_URL = "https://www.glassdoor.com/Job/jobs.htm"

    async def setup(self) -> None:
        await super().setup()
        # TODO: launch Playwright, log in with credentials

    async def teardown(self) -> None:
        await super().teardown()

    async def search(self, search_filter: JobSearchFilter) -> AsyncIterator[Job]:
        logger.info(f"[Glassdoor] Starting search: {search_filter.keywords}")
        # TODO: navigate search, parse listing cards, handle pagination
        raise NotImplementedError("Glassdoor scraper not yet implemented")
        yield

    async def get_job_details(self, job: Job) -> Job:
        raise NotImplementedError
        return job
