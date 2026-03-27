from __future__ import annotations

from typing import AsyncIterator

from loguru import logger

from src.models import Job, JobSearchFilter
from src.scrapers.base import BaseScraper


class ZipRecruiterScraper(BaseScraper):
    """Scrapes job listings from ZipRecruiter."""

    board_name = "ZipRecruiter"
    board_slug = "ziprecruiter"
    requires_auth = False

    BASE_URL = "https://www.ziprecruiter.com"
    JOBS_SEARCH_URL = "https://www.ziprecruiter.com/jobs-search"

    async def search(self, search_filter: JobSearchFilter) -> AsyncIterator[Job]:
        logger.info(f"[ZipRecruiter] Starting search: {search_filter.keywords}")
        # TODO: implement ZipRecruiter search + pagination
        raise NotImplementedError("ZipRecruiter scraper not yet implemented")
        yield

    async def get_job_details(self, job: Job) -> Job:
        raise NotImplementedError
        return job
