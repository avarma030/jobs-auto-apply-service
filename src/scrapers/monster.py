from __future__ import annotations

from typing import AsyncIterator

from loguru import logger

from src.models import Job, JobSearchFilter
from src.scrapers.base import BaseScraper


class MonsterScraper(BaseScraper):
    """Scrapes job listings from Monster.com."""

    board_name = "Monster"
    board_slug = "monster"
    requires_auth = False

    BASE_URL = "https://www.monster.com"
    JOBS_SEARCH_URL = "https://www.monster.com/jobs/search"

    async def search(self, search_filter: JobSearchFilter) -> AsyncIterator[Job]:
        logger.info(f"[Monster] Starting search: {search_filter.keywords}")
        # TODO: implement Monster search + pagination
        raise NotImplementedError("Monster scraper not yet implemented")
        yield

    async def get_job_details(self, job: Job) -> Job:
        raise NotImplementedError
        return job
