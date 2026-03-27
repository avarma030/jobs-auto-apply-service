from __future__ import annotations

from typing import AsyncIterator

from loguru import logger

from src.models import Job, JobSearchFilter
from src.scrapers.base import BaseScraper


class GreenhouseScraper(BaseScraper):
    """Scrapes jobs hosted on Greenhouse ATS (boards.greenhouse.io).

    Greenhouse exposes a public JSON board API:
    https://boards-api.greenhouse.io/v1/boards/<company>/jobs?content=true
    """

    board_name = "Greenhouse"
    board_slug = "greenhouse"
    requires_auth = False

    API_BASE = "https://boards-api.greenhouse.io/v1/boards"

    async def search(self, search_filter: JobSearchFilter) -> AsyncIterator[Job]:
        logger.info(f"[Greenhouse] Starting search: {search_filter.keywords}")
        # TODO: iterate target companies, query Greenhouse API, filter results
        raise NotImplementedError("Greenhouse scraper not yet implemented")
        yield

    async def get_job_details(self, job: Job) -> Job:
        raise NotImplementedError
        return job
