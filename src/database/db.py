from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.database.models import ApplicationRecord, Base, JobRecord
from src.models import ApplicationStatus, Job


class Database:
    """Async SQLAlchemy database layer."""

    def __init__(self, database_url: str):
        self.engine = create_async_engine(database_url, echo=False)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def init(self) -> None:
        """Create all tables if they don't exist."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database initialised")

    async def close(self) -> None:
        await self.engine.dispose()

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    async def upsert_job(self, job: Job, user_id: int | None = None) -> JobRecord:
        """Insert or update a job record. Returns the persisted record."""
        async with self.session_factory() as session:
            # Check for existing record by URL
            result = await session.execute(select(JobRecord).where(JobRecord.url == job.url))
            record = result.scalar_one_or_none()

            if record is None:
                record = JobRecord(url=job.url)
                session.add(record)

            if user_id is not None:
                record.user_id = user_id
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
            record.posted_at = job.posted_at
            record.scraped_at = job.scraped_at
            record.application_status = job.application_status
            record.applied_at = job.applied_at
            record.notes = job.notes

            await session.commit()
            return record

    async def get_pending_jobs(self, limit: int = 100) -> list[JobRecord]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(JobRecord)
                .where(JobRecord.application_status == ApplicationStatus.PENDING)
                .limit(limit)
            )
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
    ) -> ApplicationRecord:
        async with self.session_factory() as session:
            record = ApplicationRecord(
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
