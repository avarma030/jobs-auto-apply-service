from __future__ import annotations

from typing import AsyncIterator

from loguru import logger

from src.models import Job, JobSearchFilter
from src.scrapers.base import BaseScraper


class DiceScraper(BaseScraper):
    """Scrapes tech-focused job listings from Dice.com.

    Dice exposes a public JSON search API that is straightforward to query.
    """

    board_name = "Dice"
    board_slug = "dice"
    requires_auth = False

    BASE_URL = "https://www.dice.com"
    API_URL = "https://job-search-api.svc.dhigroupinc.com/v1/dice/jobs/search"

    async def search(self, search_filter: JobSearchFilter) -> AsyncIterator[Job]:
        logger.info(f"[Dice] Starting search: {search_filter.keywords}")
        # TODO: POST to Dice search API with JSON body, paginate via offset
        raise NotImplementedError("Dice scraper not yet implemented")
        yield

    async def get_job_details(self, job: Job) -> Job:
        raise NotImplementedError
        return job
