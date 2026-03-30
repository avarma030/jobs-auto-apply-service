from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.database.models import (
    ApplicationRecord,
    Base,
    JobRecord,
    RunEventRecord,
    RunJobRecord,
    SemanticCacheRecord,
)
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
            # Check for existing record by URL within the current user scope.
            q = select(JobRecord).where(JobRecord.url == job.url)
            if user_id is not None:
                q = q.where(JobRecord.user_id == user_id)
            result = await session.execute(q)
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

            existing_status = str(getattr(record, "application_status", "") or "").lower()
            should_revive_failed_job = (
                not is_new
                and job.application_status == ApplicationStatus.PENDING
                and existing_status == ApplicationStatus.FAILED.value
            )
            if is_new or job.application_status != ApplicationStatus.PENDING or should_revive_failed_job:
                record.application_status = job.application_status
                if should_revive_failed_job:
                    record.applied_at = None
                    if job.notes is None:
                        record.notes = None
            if job.applied_at is not None:
                record.applied_at = _strip_tz(job.applied_at)
            if job.notes is not None:
                record.notes = job.notes

            await session.flush()
            if scrape_run_id:
                link = await session.get(
                    RunJobRecord,
                    {"run_id": scrape_run_id, "job_id": record.id},
                )
                if link is None:
                    session.add(RunJobRecord(run_id=scrape_run_id, job_id=record.id))

            await session.commit()
            return record

    async def get_pending_jobs(
        self,
        limit: int = 100,
        user_id: int | None = None,
        scrape_run_id: str | None = None,
    ) -> list[JobRecord]:
        async with self.session_factory() as session:
            if scrape_run_id is not None:
                linked_q = (
                    select(JobRecord)
                    .join(RunJobRecord, RunJobRecord.job_id == JobRecord.id)
                    .where(
                        RunJobRecord.run_id == scrape_run_id,
                        JobRecord.application_status == ApplicationStatus.PENDING,
                    )
                    .order_by(JobRecord.scraped_at.desc())
                )
                if user_id is not None:
                    linked_q = linked_q.where(JobRecord.user_id == user_id)
                linked_records = list((await session.execute(linked_q)).scalars().all())

                legacy_q = (
                    select(JobRecord)
                    .where(
                        JobRecord.application_status == ApplicationStatus.PENDING,
                        JobRecord.scrape_run_id == scrape_run_id,
                    )
                    .order_by(JobRecord.scraped_at.desc())
                )
                if user_id is not None:
                    legacy_q = legacy_q.where(JobRecord.user_id == user_id)
                legacy_records = list((await session.execute(legacy_q)).scalars().all())

                merged: dict[int, JobRecord] = {}
                for record in linked_records + legacy_records:
                    merged.setdefault(record.id, record)
                ordered = sorted(
                    merged.values(),
                    key=lambda record: record.scraped_at or datetime.min,
                    reverse=True,
                )
                return ordered[:limit]

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

    async def append_run_event(
        self,
        run_id: str,
        *,
        user_id: int | None = None,
        event_type: str = "progress",
        level: str = "info",
        message: str | None = None,
        status: str | None = None,
        jobs_found: int | None = None,
        jobs_applied: int | None = None,
        payload: dict | None = None,
    ) -> RunEventRecord:
        async with self.session_factory() as session:
            record = RunEventRecord(
                run_id=run_id,
                user_id=user_id,
                event_type=event_type,
                level=level,
                message=message,
                status=status,
                jobs_found=jobs_found,
                jobs_applied=jobs_applied,
                payload_json=json.dumps(payload) if payload is not None else None,
            )
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record

    async def get_run_events(
        self,
        run_id: str,
        *,
        user_id: int | None = None,
        after_id: int = 0,
        limit: int = 500,
    ) -> list[RunEventRecord]:
        async with self.session_factory() as session:
            q = (
                select(RunEventRecord)
                .where(
                    RunEventRecord.run_id == run_id,
                    RunEventRecord.id > after_id,
                )
                .order_by(RunEventRecord.id.asc())
                .limit(limit)
            )
            if user_id is not None:
                q = q.where(RunEventRecord.user_id == user_id)
            result = await session.execute(q)
            return list(result.scalars().all())

    async def get_semantic_cache(
        self,
        key: str,
        source_hash: str | None = None,
    ) -> dict | None:
        async with self.session_factory() as session:
            record = await session.get(SemanticCacheRecord, key)
            if record is None:
                return None
            if source_hash is not None and record.source_hash != source_hash:
                return None
            try:
                return json.loads(record.payload_json)
            except json.JSONDecodeError:
                logger.warning(f"Invalid semantic cache payload for key '{key}'")
                return None

    async def upsert_semantic_cache(
        self,
        key: str,
        *,
        kind: str,
        source_hash: str,
        payload: dict,
        user_id: int | None = None,
    ) -> None:
        async with self.session_factory() as session:
            record = await session.get(SemanticCacheRecord, key)
            if record is None:
                record = SemanticCacheRecord(key=key, kind=kind, source_hash=source_hash)
                session.add(record)
            record.kind = kind
            record.user_id = user_id
            record.source_hash = source_hash
            record.payload_json = json.dumps(payload)
            await session.commit()

    async def get_recent_match_examples(
        self,
        user_id: int,
        *,
        limit: int = 12,
        exclude_job_id: int | None = None,
    ) -> list[JobRecord]:
        async with self.session_factory() as session:
            q = select(JobRecord).where(
                JobRecord.user_id == user_id,
                JobRecord.description.is_not(None),
                JobRecord.application_status.in_(
                    [
                        ApplicationStatus.APPLIED,
                        ApplicationStatus.INTERVIEWED,
                        ApplicationStatus.OFFERED,
                    ]
                ),
            )
            if exclude_job_id is not None:
                q = q.where(JobRecord.id != exclude_job_id)
            q = q.order_by(JobRecord.applied_at.desc(), JobRecord.scraped_at.desc()).limit(limit)
            result = await session.execute(q)
            return list(result.scalars().all())

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
