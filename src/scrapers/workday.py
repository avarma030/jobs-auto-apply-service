from __future__ import annotations

from typing import AsyncIterator

from loguru import logger

from src.models import Job, JobSearchFilter
from src.scrapers.base import BaseScraper


class WorkdayScraper(BaseScraper):
    """Scrapes jobs hosted on Workday career portals.

    Workday uses a REST-like JSON API under the path:
    https://<company>.wd5.myworkdayjobs.com/wday/cxs/<company>/<board>/jobs
    This scraper handles both the listing and detail fetch.
    """

    board_name = "Workday"
    board_slug = "workday"
    requires_auth = False

    async def search(self, search_filter: JobSearchFilter) -> AsyncIterator[Job]:
        logger.info(f"[Workday] Starting search: {search_filter.keywords}")
        # TODO: accept a list of Workday tenant URLs, query each, filter, yield
        raise NotImplementedError("Workday scraper not yet implemented")
        yield

    async def get_job_details(self, job: Job) -> Job:
        raise NotImplementedError
        return job
