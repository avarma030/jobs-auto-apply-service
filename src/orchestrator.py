from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Type

from loguru import logger

from src.appliers.base import BaseApplier
from src.appliers.generic import GenericApplier
from src.appliers.greenhouse import GreenhouseApplier
from src.appliers.lever import LeverApplier
from src.appliers.linkedin import LinkedInApplier
from src.appliers.workday import WorkdayApplier
from src.config import settings
from src.database import Database
from src.models import ApplicationStatus, Job, JobSearchFilter, UserProfile
from src.scrapers.base import BaseScraper
from src.scrapers.dice import DiceScraper
from src.scrapers.glassdoor import GlassdoorScraper
from src.scrapers.greenhouse import GreenhouseScraper
from src.scrapers.indeed import IndeedScraper
from src.scrapers.lever import LeverScraper
from src.scrapers.linkedin import LinkedInScraper
from src.scrapers.monster import MonsterScraper
from src.scrapers.workday import WorkdayScraper
from src.scrapers.ziprecruiter import ZipRecruiterScraper

SCRAPER_REGISTRY: dict[str, Type[BaseScraper]] = {
    "linkedin": LinkedInScraper,
    "indeed": IndeedScraper,
    "glassdoor": GlassdoorScraper,
    "ziprecruiter": ZipRecruiterScraper,
    "dice": DiceScraper,
    "monster": MonsterScraper,
    "lever": LeverScraper,
    "greenhouse": GreenhouseScraper,
    "workday": WorkdayScraper,
}

APPLIER_REGISTRY: list[Type[BaseApplier]] = [
    LinkedInApplier,
    GreenhouseApplier,
    LeverApplier,
    WorkdayApplier,
    GenericApplier,  # fallback — must be last
]


class Orchestrator:
    """Coordinates scraping and applying across all enabled job boards."""

    def __init__(self, profile: UserProfile, db: Database):
        self.profile = profile
        self.db = db

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_scrape(self, search_filter: JobSearchFilter) -> int:
        """Scrape all enabled boards and persist discovered jobs. Returns count."""
        boards = settings.enabled_board_list()
        logger.info(f"Scraping {len(boards)} board(s): {boards}")

        total = 0
        tasks = [
            self._scrape_board(board, search_filter)
            for board in boards
            if board in SCRAPER_REGISTRY
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.error(f"Scrape task failed: {r}")
            else:
                total += r  # type: ignore[operator]
        logger.info(f"Scraping complete — {total} new jobs found")
        return total

    async def run_apply(self) -> dict:
        """Apply to all pending jobs. Returns status counts."""
        pending = await self.db.get_pending_jobs(limit=settings.max_applications_per_run)
        logger.info(f"Applying to {len(pending)} pending jobs")

        counts: dict[str, int] = {}
        for record in pending:
            job = self._record_to_job(record)

            if settings.dry_run:
                logger.info(f"[DRY RUN] Would apply to {job.title} @ {job.company}")
                counts["dry_run"] = counts.get("dry_run", 0) + 1
                continue

            applier = self._pick_applier(job)
            async with applier as a:
                result = await a.apply(job)

            status = result.status
            counts[status] = counts.get(status, 0) + 1
            await self.db.update_job_status(
                record.id,
                status,
                applied_at=datetime.utcnow() if status == ApplicationStatus.APPLIED else None,
                notes=result.message,
            )
            await self.db.log_application(
                record.id,
                status,
                confirmation_id=result.confirmation_id,
                message=result.message,
            )

        logger.info(f"Apply run complete: {counts}")
        return counts

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _scrape_board(self, board: str, search_filter: JobSearchFilter) -> int:
        scraper_cls = SCRAPER_REGISTRY[board]
        creds = self._get_creds(board)
        count = 0
        try:
            async with scraper_cls(credentials=creds) as scraper:
                async for job in scraper.search(search_filter):
                    await self.db.upsert_job(job)
                    count += 1
        except NotImplementedError:
            logger.warning(f"[{board}] Scraper not yet implemented — skipping")
        except Exception as exc:
            logger.error(f"[{board}] Scraper error: {exc}")
        return count

    def _pick_applier(self, job: Job) -> BaseApplier:
        for cls in APPLIER_REGISTRY:
            instance = cls(profile=self.profile)
            if instance.can_apply(job):
                return instance
        return GenericApplier(profile=self.profile)

    def _get_creds(self, board: str) -> dict:
        accounts = self.profile.job_board_accounts
        cred_obj = getattr(accounts, board, None)
        if cred_obj is None:
            return {}
        return cred_obj.model_dump(exclude_none=True)

    @staticmethod
    def _record_to_job(record) -> Job:  # type: ignore[no-untyped-def]
        import json as _json

        return Job(
            id=str(record.id),
            title=record.title,
            company=record.company,
            location=record.location,
            description=record.description,
            url=record.url,
            source_board=record.source_board,
            external_id=record.external_id,
            job_type=record.job_type,
            work_mode=record.work_mode,
            experience_level=record.experience_level,
            salary_min=record.salary_min,
            salary_max=record.salary_max,
            salary_currency=record.salary_currency,
            skills=_json.loads(record.skills) if record.skills else [],
            easy_apply=record.easy_apply,
            posted_at=record.posted_at,
            scraped_at=record.scraped_at,
            application_status=record.application_status,
            applied_at=record.applied_at,
            notes=record.notes,
        )
