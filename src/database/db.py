from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.database.models import ApplicationRecord, Base, JobRecord
from src.models import ApplicationStatus, Job


def _strip_tz(dt: datetime | None) -> datetime | None:
    """Convert timezone-aware datetime to naive UTC for TIMESTAMP columns."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


class Database:
    """Async SQLAlchemy database layer."""

    def __init__(self, database_url: str):
        self.engine = create_async_engine(database_url, echo=False)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def init(self) -> None:
        """Create all tables if they don't exist."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await self._ensure_scrape_run_columns(conn)
        logger.info("Database initialised")

    async def close(self) -> None:
        await self.engine.dispose()

    async def _ensure_scrape_run_columns(self, conn) -> None:
        def _existing_columns(sync_conn) -> set[str]:
            inspector = inspect(sync_conn)
            return {
                column["name"]
                for column in inspector.get_columns("scrape_runs")
            }

        existing_columns = await conn.run_sync(_existing_columns)
        statements: list[str] = []
        if "trigger_type" not in existing_columns:
            statements.append("ALTER TABLE scrape_runs ADD COLUMN trigger_type VARCHAR(32)")
        if "search_criteria_json" not in existing_columns:
            statements.append("ALTER TABLE scrape_runs ADD COLUMN search_criteria_json TEXT")
        if "saved_search_key" not in existing_columns:
            statements.append("ALTER TABLE scrape_runs ADD COLUMN saved_search_key VARCHAR(64)")

        for statement in statements:
            await conn.execute(text(statement))

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    async def upsert_job(
        self, job: Job, user_id: int | None = None, scrape_run_id: str | None = None
    ) -> JobRecord:
        """Insert or update a job record. Returns the persisted record."""
        async with self.session_factory() as session:
            # Check for existing record by URL
            result = await session.execute(select(JobRecord).where(JobRecord.url == job.url))
            record = result.scalar_one_or_none()

            is_new = record is None
            if is_new:
                record = JobRecord(url=job.url)
                session.add(record)

            if user_id is not None:
                record.user_id = user_id
            # Only stamp scrape_run_id on new records — don't overwrite on re-scrape
            if is_new and scrape_run_id:
                record.scrape_run_id = scrape_run_id
            record.external_id = job.external_id
            record.source_board = job.source_board
            record.title = job.title
            record.company = job.company
            record.location = job.location
            record.description = job.description
            record.job_type = job.job_type
            record.work_mode = job.work_mode
            record.experience_level = job.experience_level
            record.salary_min = job.salary_min
            record.salary_max = job.salary_max
            record.salary_currency = job.salary_currency
            record.skills = json.dumps(job.skills)
            record.easy_apply = job.easy_apply
            record.posted_at = _strip_tz(job.posted_at)
            record.scraped_at = _strip_tz(job.scraped_at)
            if is_new or job.application_status != ApplicationStatus.PENDING:
                record.application_status = job.application_status
            if job.applied_at is not None:
                record.applied_at = _strip_tz(job.applied_at)
            if job.notes is not None:
                record.notes = job.notes

            await session.commit()
            return record

    async def get_pending_jobs(
        self,
        limit: int = 100,
        user_id: int | None = None,
        scrape_run_id: str | None = None,
    ) -> list[JobRecord]:
        async with self.session_factory() as session:
            q = select(JobRecord).where(
                JobRecord.application_status == ApplicationStatus.PENDING
            )
            if user_id is not None:
                q = q.where(JobRecord.user_id == user_id)
            if scrape_run_id is not None:
                # Scope to the current run — never process other runs' jobs
                q = q.where(JobRecord.scrape_run_id == scrape_run_id)
            else:
                # Legacy / standalone path: limit to jobs scraped in the last 24 h
                # so very old pending jobs don't accumulate indefinitely
                cutoff = datetime.utcnow() - timedelta(hours=24)
                q = q.where(JobRecord.scraped_at >= cutoff)
            result = await session.execute(q.limit(limit))
            return list(result.scalars().all())

    async def update_job_status(
        self,
        job_id: int,
        status: ApplicationStatus,
        applied_at: Optional[datetime] = None,
        notes: Optional[str] = None,
    ) -> None:
        async with self.session_factory() as session:
            result = await session.execute(select(JobRecord).where(JobRecord.id == job_id))
            record = result.scalar_one_or_none()
            if record:
                record.application_status = status
                if applied_at:
                    record.applied_at = applied_at
                if notes:
                    record.notes = notes
                await session.commit()

    async def update_job_details(self, job_id: int, job: Job) -> None:
        """Persist enriched job details (description, salary, skills, etc.)."""
        async with self.session_factory() as session:
            result = await session.execute(select(JobRecord).where(JobRecord.id == job_id))
            record = result.scalar_one_or_none()
            if record:
                record.description = job.description
                record.easy_apply = job.easy_apply
                if job.salary_min is not None:
                    record.salary_min = job.salary_min
                if job.salary_max is not None:
                    record.salary_max = job.salary_max
                if job.salary_currency:
                    record.salary_currency = job.salary_currency
                if job.skills:
                    record.skills = json.dumps(job.skills)
                if job.job_type:
                    record.job_type = job.job_type
                if job.experience_level:
                    record.experience_level = job.experience_level
                await session.commit()

    # ------------------------------------------------------------------
    # Application log
    # ------------------------------------------------------------------

    async def update_job_ai_fields(
        self,
        job_id: int,
        match_score: float | None = None,
        ats_score: float | None = None,
        ats_type: str | None = None,
        tailored_resume_path: str | None = None,
        cover_letter_path: str | None = None,
    ) -> None:
        """Persist AI-pipeline results to the job record."""
        async with self.session_factory() as session:
            result = await session.execute(select(JobRecord).where(JobRecord.id == job_id))
            record = result.scalar_one_or_none()
            if record:
                if match_score is not None:
                    record.match_score = match_score
                if ats_score is not None:
                    record.ats_score = ats_score
                if ats_type is not None:
                    record.ats_type = ats_type
                if tailored_resume_path is not None:
                    record.tailored_resume_path = tailored_resume_path
                if cover_letter_path is not None:
                    record.cover_letter_path = cover_letter_path
                await session.commit()

    async def log_application(
        self,
        job_id: int,
        status: ApplicationStatus,
        confirmation_id: Optional[str] = None,
        message: Optional[str] = None,
        user_id: int | None = None,
    ) -> ApplicationRecord:
        async with self.session_factory() as session:
            record = ApplicationRecord(
                user_id=user_id,
                job_id=job_id,
                status=status,
                confirmation_id=confirmation_id,
                message=message,
            )
            session.add(record)
            await session.commit()
            return record

    async def update_profile_custom_answers(
        self, user_id: int, custom_answers: dict[str, str]
    ) -> None:
        """Merge new custom_answers into the user's stored profile JSON blob."""
        from src.database.models import UserProfile as UserProfileRecord

        async with self.session_factory() as session:
            row = (
                await session.execute(
                    select(UserProfileRecord).where(UserProfileRecord.user_id == user_id)
                )
            ).scalar_one_or_none()
            if row:
                profile_data = json.loads(row.profile_json) if row.profile_json else {}
                existing = profile_data.get("custom_answers", {})
                existing.update(custom_answers)
                profile_data["custom_answers"] = existing
                row.profile_json = json.dumps(profile_data)
                await session.commit()
                logger.debug(f"Persisted {len(custom_answers)} custom_answers for user {user_id}")

    async def get_application_stats(self) -> dict:
        async with self.session_factory() as session:
            from sqlalchemy import func

            result = await session.execute(
                select(JobRecord.application_status, func.count(JobRecord.id))
                .group_by(JobRecord.application_status)
            )
            return dict(result.all())
