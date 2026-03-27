from __future__ import annotations

from typing import AsyncIterator

from loguru import logger

from src.models import Job, JobSearchFilter
from src.scrapers.base import BaseScraper


class LeverScraper(BaseScraper):
    """Scrapes jobs hosted on Lever ATS (jobs.lever.co).

    Lever provides a public JSON API for each company's job board at:
    https://api.lever.co/v0/postings/<company>?mode=json
    This scraper is designed to be called per-company rather than as a
    global search, but can be combined with a company list.
    """

    board_name = "Lever"
    board_slug = "lever"
    requires_auth = False

    API_BASE = "https://api.lever.co/v0/postings"

    async def search(self, search_filter: JobSearchFilter) -> AsyncIterator[Job]:
        logger.info(f"[Lever] Starting search: {search_filter.keywords}")
        # TODO: iterate over a list of target companies, query their Lever API,
        # filter by keywords / work mode
        raise NotImplementedError("Lever scraper not yet implemented")
        yield

    async def get_job_details(self, job: Job) -> Job:
        raise NotImplementedError
        return job
