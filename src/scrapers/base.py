from __future__ import annotations

import abc
from typing import AsyncIterator

from loguru import logger

from src.models import Job, JobSearchFilter


class BaseScraper(abc.ABC):
    """Abstract base class for all job-board scrapers.

    Each concrete scraper targets one job board and is responsible for:
    - Authenticating (if required)
    - Searching for jobs matching the filter
    - Parsing results into ``Job`` objects
    """

    #: Human-readable name of the board, e.g. "LinkedIn"
    board_name: str = ""
    #: Slug used as ``Job.source_board``, e.g. "linkedin"
    board_slug: str = ""
    #: Whether the board requires a logged-in session to scrape
    requires_auth: bool = False

    def __init__(self, credentials: dict | None = None):
        self.credentials = credentials or {}
        self._session_active = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "BaseScraper":
        await self.setup()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.teardown()

    async def setup(self) -> None:
        """Initialise HTTP sessions, browser contexts, etc."""
        logger.debug(f"[{self.board_name}] Setting up scraper")

    async def teardown(self) -> None:
        """Release resources."""
        logger.debug(f"[{self.board_name}] Tearing down scraper")

    # ------------------------------------------------------------------
    # Core interface
    # ------------------------------------------------------------------

    @abc.abstractmethod
    async def search(self, search_filter: JobSearchFilter) -> AsyncIterator[Job]:
        """Yield ``Job`` objects matching *search_filter*.

        Implementations should handle pagination internally and yield
        jobs as they are scraped so callers can process them incrementally.
        """
        ...

    @abc.abstractmethod
    async def get_job_details(self, job: Job) -> Job:
        """Fetch and enrich a ``Job`` with full description / metadata."""
        ...

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_job(self, **kwargs: object) -> Job:
        return Job(source_board=self.board_slug, **kwargs)  # type: ignore[arg-type]

    def _log(self, msg: str, level: str = "info") -> None:
        getattr(logger, level)(f"[{self.board_name}] {msg}")
