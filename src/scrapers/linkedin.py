from __future__ import annotations

from typing import AsyncIterator

from loguru import logger

from src.models import Job, JobSearchFilter
from src.scrapers.base import BaseScraper


class LinkedInScraper(BaseScraper):
    """Scrapes job listings from LinkedIn.

    Strategy:
    - Uses the LinkedIn Jobs search API (unauthenticated guest endpoint) for
      initial listing pages.
    - Falls back to Playwright-driven browser automation for full details and
      Easy Apply jobs when credentials are provided.
    """

    board_name = "LinkedIn"
    board_slug = "linkedin"
    requires_auth = False  # guest scraping supported; auth unlocks Easy Apply

    BASE_URL = "https://www.linkedin.com"
    JOBS_SEARCH_URL = "https://www.linkedin.com/jobs/search/"

    async def setup(self) -> None:
        await super().setup()
        # TODO: initialise Playwright browser + optional login

    async def teardown(self) -> None:
        # TODO: close browser
        await super().teardown()

    async def search(self, search_filter: JobSearchFilter) -> AsyncIterator[Job]:
        """Yield jobs matching *search_filter* from LinkedIn."""
        logger.info(f"[LinkedIn] Starting search: {search_filter.keywords}")
        # TODO: implement paginated LinkedIn job search
        # 1. Build search URL params from search_filter
        # 2. Fetch listing pages with httpx / Playwright
        # 3. Parse each card with BeautifulSoup
        # 4. yield self._make_job(...)
        raise NotImplementedError("LinkedIn scraper not yet implemented")
        yield  # make this an async generator

    async def get_job_details(self, job: Job) -> Job:
        """Fetch the full job description page and enrich *job*."""
        # TODO: fetch job.url, parse description, skills, salary
        raise NotImplementedError
        return job


class LinkedInApplier:
    """Handles Easy Apply and external apply flows on LinkedIn.

    Import from src.appliers.linkedin to keep concerns separated.
    Stub kept here for discoverability.
    """
