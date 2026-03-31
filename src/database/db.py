from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.database.models import (
    ApplicationRecord,
    AnswerMemoryRecord,
    CandidateKnowledgePackRecord,
    JobRecord,
    JobKnowledgePackRecord,
    RunEventRecord,
    RunExecutionRecord,
    RunJobRecord,
    SemanticCacheRecord,
)
from src.database.schema import ensure_database_schema, get_schema_status
from src.models import ApplicationStatus, Job
from src.services.job_identity import normalize_job_url


def _strip_tz(dt: datetime | None) -> datetime | None:
    """Convert timezone-aware datetime to naive UTC for TIMESTAMP columns."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _job_status_priority(status: str | None) -> int:
    lowered = (status or "").lower()
    if lowered in {
        ApplicationStatus.APPLIED.value,
        ApplicationStatus.INTERVIEWED.value,
        ApplicationStatus.OFFERED.value,
    }:
        return 3
    if lowered == ApplicationStatus.PENDING.value:
        return 2
    if lowered in {ApplicationStatus.FAILED.value, ApplicationStatus.SKIPPED.value}:
        return 1
    return 0


def _job_record_priority(record: JobRecord) -> tuple[int, datetime, datetime, int]:
    return (
        _job_status_priority(record.application_status),
        record.applied_at or datetime.min,
        record.scraped_at or datetime.min,
        record.id or 0,
    )


class Database:
    """Async SQLAlchemy database layer."""

    def __init__(self, database_url: str, *, auto_migrate: bool = False):
        self.database_url = database_url
        self.auto_migrate = auto_migrate
        self.engine = create_async_engine(database_url, echo=False)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def init(self) -> None:
        """Ensure the database is at the expected Alembic revision."""
        status = await ensure_database_schema(
            self.engine,
            self.database_url,
            auto_migrate=self.auto_migrate,
        )
        logger.info(f"Database initialised (schema={status.current_revision})")

    async def close(self) -> None:
        await self.engine.dispose()

    async def schema_status(self) -> dict[str, object]:
        status = await get_schema_status(self.engine, self.database_url)
        return {
            "current_revision": status.current_revision,
            "head_revision": status.head_revision,
            "at_head": status.at_head,
            "is_empty": status.is_empty,
            "has_legacy_schema": status.has_legacy_schema,
        }

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    async def upsert_job(
        self, job: Job, user_id: int | None = None, scrape_run_id: str | None = None
    ) -> JobRecord:
        """Insert or update a job record. Returns the persisted record."""
        normalized_url = normalize_job_url(job.url) or job.url.strip()
        async with self.session_factory() as session:
            match_filters = [JobRecord.normalized_url == normalized_url]
            if job.external_id:
                match_filters.append(
                    and_(
                        JobRecord.source_board == job.source_board,
                        JobRecord.external_id == job.external_id,
                    )
                )

            q = select(JobRecord).where(or_(*match_filters))
            if user_id is not None:
                q = q.where(JobRecord.user_id == user_id)
            else:
                q = q.where(JobRecord.user_id.is_(None))
            matches = list((await session.execute(q)).scalars().all())
            record = None
            if matches:
                matches.sort(key=_job_record_priority, reverse=True)
                record = matches[0]
                duplicates = matches[1:]
                if duplicates:
                    await self._merge_duplicate_jobs(session, record, duplicates)

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
            record.url = job.url
            record.normalized_url = normalized_url
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

    async def _merge_duplicate_jobs(
        self,
        session: AsyncSession,
        primary: JobRecord,
        duplicates: list[JobRecord],
    ) -> None:
        for duplicate in duplicates:
            if primary.scrape_run_id is None and duplicate.scrape_run_id is not None:
                primary.scrape_run_id = duplicate.scrape_run_id
            if primary.normalized_url is None and duplicate.normalized_url is not None:
                primary.normalized_url = duplicate.normalized_url

            linked_run_ids = list(
                (
                    await session.execute(
                        select(RunJobRecord.run_id).where(RunJobRecord.job_id == duplicate.id)
                    )
                ).scalars().all()
            )
            for run_id in linked_run_ids:
                existing_link = await session.get(
                    RunJobRecord,
                    {"run_id": run_id, "job_id": primary.id},
                )
                if existing_link is None:
                    session.add(RunJobRecord(run_id=run_id, job_id=primary.id))

            application_rows = list(
                (
                    await session.execute(
                        select(ApplicationRecord).where(ApplicationRecord.job_id == duplicate.id)
                    )
                ).scalars().all()
            )
            for application in application_rows:
                application.job_id = primary.id

            await session.execute(
                delete(RunJobRecord).where(RunJobRecord.job_id == duplicate.id)
            )
            await session.delete(duplicate)

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

    async def enqueue_run_execution(
        self,
        run_id: str,
        *,
        user_id: int,
        request_payload: dict,
    ) -> bool:
        async with self.session_factory() as session:
            existing = await session.get(RunExecutionRecord, run_id)
            if existing is not None:
                return False

            session.add(
                RunExecutionRecord(
                    run_id=run_id,
                    user_id=user_id,
                    request_payload_json=json.dumps(request_payload, sort_keys=True),
                    state="queued",
                )
            )
            await session.commit()
            return True

    async def mark_run_dispatched(self, run_id: str) -> bool:
        async with self.session_factory() as session:
            record = await session.get(RunExecutionRecord, run_id)
            if record is None:
                return False
            if record.state in {"completed", "failed", "cancelled"}:
                return False
            record.state = "dispatched"
            record.dispatch_attempts += 1
            record.dispatched_at = datetime.utcnow()
            await session.commit()
            return True

    async def claim_run_execution(
        self,
        run_id: str,
        *,
        worker_id: str,
    ) -> dict[str, object] | None:
        async with self.session_factory() as session:
            record = await session.get(RunExecutionRecord, run_id)
            if record is None:
                return None

            if record.cancel_requested_at is not None:
                if record.state not in {"completed", "failed", "cancelled"}:
                    record.state = "cancelled"
                    record.finished_at = datetime.utcnow()
                    await session.commit()
                return None

            if record.state not in {"queued", "dispatched", "retrying"}:
                return None

            now = datetime.utcnow()
            record.state = "running"
            record.worker_id = worker_id
            record.execution_attempts += 1
            record.claimed_at = now
            record.heartbeat_at = now
            if record.started_at is None:
                record.started_at = now
            await session.commit()

            try:
                payload = json.loads(record.request_payload_json)
            except json.JSONDecodeError:
                payload = {}
            return {
                "run_id": record.run_id,
                "user_id": record.user_id,
                "request_payload": payload,
            }

    async def claim_next_run_execution(
        self,
        *,
        worker_id: str,
        limit: int = 20,
    ) -> dict[str, object] | None:
        async with self.session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(RunExecutionRecord.run_id)
                        .where(RunExecutionRecord.state.in_(("queued", "dispatched", "retrying")))
                        .order_by(RunExecutionRecord.created_at.asc())
                        .limit(limit)
                    )
                ).scalars().all()
            )

        for run_id in rows:
            claimed = await self.claim_run_execution(run_id, worker_id=worker_id)
            if claimed is not None:
                return claimed
        return None

    async def heartbeat_run_execution(self, run_id: str, *, worker_id: str) -> bool:
        async with self.session_factory() as session:
            record = await session.get(RunExecutionRecord, run_id)
            if record is None or record.worker_id != worker_id or record.state != "running":
                return False
            record.heartbeat_at = datetime.utcnow()
            await session.commit()
            return True

    async def complete_run_execution(
        self,
        run_id: str,
        *,
        worker_id: str | None,
        state: str,
        last_error: str | None = None,
    ) -> bool:
        async with self.session_factory() as session:
            record = await session.get(RunExecutionRecord, run_id)
            if record is None:
                return False
            if worker_id is not None:
                record.worker_id = worker_id
            record.state = state
            record.last_error = last_error
            record.heartbeat_at = datetime.utcnow()
            record.finished_at = datetime.utcnow()
            await session.commit()
            return True

    async def request_run_cancellation(self, run_id: str, *, user_id: int | None = None) -> str | None:
        async with self.session_factory() as session:
            record = await session.get(RunExecutionRecord, run_id)
            if record is None:
                return None
            if user_id is not None and record.user_id != user_id:
                return None
            if record.cancel_requested_at is None:
                record.cancel_requested_at = datetime.utcnow()
            if record.state in {"queued", "dispatched", "retrying"}:
                record.state = "cancelled"
                record.finished_at = datetime.utcnow()
            await session.commit()
            return record.state

    async def is_run_cancellation_requested(self, run_id: str) -> bool:
        async with self.session_factory() as session:
            record = await session.get(RunExecutionRecord, run_id)
            return bool(record and record.cancel_requested_at is not None)

    async def get_run_execution(self, run_id: str) -> RunExecutionRecord | None:
        async with self.session_factory() as session:
            return await session.get(RunExecutionRecord, run_id)

    async def get_queue_health(self) -> dict[str, int]:
        async with self.session_factory() as session:
            states = ["queued", "dispatched", "running", "retrying", "completed", "failed", "cancelled"]
            counts: dict[str, int] = {}
            for state in states:
                counts[state] = (
                    await session.execute(
                        select(func.count())
                        .select_from(RunExecutionRecord)
                        .where(RunExecutionRecord.state == state)
                    )
                ).scalar_one()
            counts["cancel_requested"] = (
                await session.execute(
                    select(func.count())
                    .select_from(RunExecutionRecord)
                    .where(RunExecutionRecord.cancel_requested_at.is_not(None))
                )
            ).scalar_one()
            return counts

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
        prompt_name: str | None = None,
        prompt_version: str | None = None,
        model_name: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        async with self.session_factory() as session:
            record = await session.get(SemanticCacheRecord, key)
            if record is None:
                record = SemanticCacheRecord(key=key, kind=kind, source_hash=source_hash)
                session.add(record)
            record.kind = kind
            record.user_id = user_id
            record.source_hash = source_hash
            record.prompt_name = prompt_name
            record.prompt_version = prompt_version
            record.model_name = model_name
            record.payload_json = json.dumps(payload)
            record.metadata_json = json.dumps(metadata) if metadata is not None else None
            await session.commit()

    async def get_candidate_knowledge_pack(
        self,
        user_id: int,
        *,
        version: str,
        source_hash: str,
    ) -> dict | None:
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    select(CandidateKnowledgePackRecord).where(
                        CandidateKnowledgePackRecord.user_id == user_id,
                        CandidateKnowledgePackRecord.version == version,
                        CandidateKnowledgePackRecord.source_hash == source_hash,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            try:
                payload = json.loads(row.payload_json)
            except json.JSONDecodeError:
                logger.warning(
                    f"Invalid candidate knowledge payload for user {user_id} and hash {source_hash}"
                )
                return None
            try:
                payload["_embedding_json"] = json.loads(row.embedding_json) if row.embedding_json else None
            except json.JSONDecodeError:
                payload["_embedding_json"] = None
            payload["_embedding_model"] = row.embedding_model
            payload["_source_hash"] = row.source_hash
            payload["_version"] = row.version
            return payload

    async def upsert_candidate_knowledge_pack(
        self,
        user_id: int,
        *,
        version: str,
        source_hash: str,
        payload: dict,
        embedding: list[float] | None = None,
        embedding_model: str | None = None,
    ) -> None:
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    select(CandidateKnowledgePackRecord).where(
                        CandidateKnowledgePackRecord.user_id == user_id,
                        CandidateKnowledgePackRecord.version == version,
                        CandidateKnowledgePackRecord.source_hash == source_hash,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = CandidateKnowledgePackRecord(
                    user_id=user_id,
                    version=version,
                    source_hash=source_hash,
                )
                session.add(row)
            row.payload_json = json.dumps(payload)
            row.embedding_json = json.dumps(embedding) if embedding is not None else None
            row.embedding_model = embedding_model
            await session.commit()

    async def get_job_knowledge_pack(
        self,
        job_id: int,
        *,
        version: str,
        source_hash: str,
    ) -> dict | None:
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    select(JobKnowledgePackRecord).where(
                        JobKnowledgePackRecord.job_id == job_id,
                        JobKnowledgePackRecord.version == version,
                        JobKnowledgePackRecord.source_hash == source_hash,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            try:
                payload = json.loads(row.payload_json)
            except json.JSONDecodeError:
                logger.warning(
                    f"Invalid job knowledge payload for job {job_id} and hash {source_hash}"
                )
                return None
            try:
                payload["_embedding_json"] = json.loads(row.embedding_json) if row.embedding_json else None
            except json.JSONDecodeError:
                payload["_embedding_json"] = None
            payload["_embedding_model"] = row.embedding_model
            payload["_source_hash"] = row.source_hash
            payload["_version"] = row.version
            return payload

    async def upsert_job_knowledge_pack(
        self,
        job_id: int,
        *,
        user_id: int | None,
        version: str,
        source_hash: str,
        payload: dict,
        embedding: list[float] | None = None,
        embedding_model: str | None = None,
    ) -> None:
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    select(JobKnowledgePackRecord).where(
                        JobKnowledgePackRecord.job_id == job_id,
                        JobKnowledgePackRecord.version == version,
                        JobKnowledgePackRecord.source_hash == source_hash,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = JobKnowledgePackRecord(
                    job_id=job_id,
                    user_id=user_id,
                    version=version,
                    source_hash=source_hash,
                )
                session.add(row)
            row.user_id = user_id
            row.payload_json = json.dumps(payload)
            row.embedding_json = json.dumps(embedding) if embedding is not None else None
            row.embedding_model = embedding_model
            await session.commit()

    async def upsert_answer_memory(
        self,
        *,
        user_id: int,
        question_key: str,
        question_text: str,
        answer_text: str,
        answer_type: str = "text",
        source_kind: str = "learned",
        confidence: float = 1.0,
        approved: bool = True,
        evidence: dict | list | None = None,
        embedding: list[float] | None = None,
        embedding_model: str | None = None,
    ) -> None:
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    select(AnswerMemoryRecord).where(
                        AnswerMemoryRecord.user_id == user_id,
                        AnswerMemoryRecord.question_key == question_key,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = AnswerMemoryRecord(user_id=user_id, question_key=question_key)
                session.add(row)
            row.question_text = question_text
            row.answer_text = answer_text
            row.answer_type = answer_type
            row.source_kind = source_kind
            row.confidence = confidence
            row.approved = approved
            row.evidence_json = json.dumps(evidence) if evidence is not None else None
            row.embedding_json = json.dumps(embedding) if embedding is not None else None
            row.embedding_model = embedding_model
            await session.commit()

    async def get_answer_memory_entries(
        self,
        *,
        user_id: int,
        question_keys: list[str] | None = None,
        limit: int = 100,
    ) -> list[dict]:
        async with self.session_factory() as session:
            q = select(AnswerMemoryRecord).where(AnswerMemoryRecord.user_id == user_id)
            if question_keys:
                q = q.where(AnswerMemoryRecord.question_key.in_(question_keys))
            q = q.order_by(
                AnswerMemoryRecord.updated_at.desc(),
                AnswerMemoryRecord.id.desc(),
            ).limit(limit)
            rows = list((await session.execute(q)).scalars().all())

        results: list[dict] = []
        for row in rows:
            try:
                evidence = json.loads(row.evidence_json) if row.evidence_json else None
            except json.JSONDecodeError:
                evidence = None
            try:
                embedding = json.loads(row.embedding_json) if row.embedding_json else None
            except json.JSONDecodeError:
                embedding = None
            results.append(
                {
                    "id": row.id,
                    "question_key": row.question_key,
                    "question_text": row.question_text,
                    "answer_text": row.answer_text,
                    "answer_type": row.answer_type,
                    "source_kind": row.source_kind,
                    "confidence": row.confidence,
                    "approved": row.approved,
                    "evidence": evidence,
                    "embedding": embedding,
                    "embedding_model": row.embedding_model,
                    "usage_count": row.usage_count,
                    "last_used_at": row.last_used_at,
                    "updated_at": row.updated_at,
                }
            )
        return results

    async def mark_answer_memory_used(self, memory_ids: list[int]) -> None:
        if not memory_ids:
            return
        async with self.session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(AnswerMemoryRecord).where(AnswerMemoryRecord.id.in_(memory_ids))
                    )
                ).scalars().all()
            )
            now = datetime.utcnow()
            for row in rows:
                row.usage_count += 1
                row.last_used_at = now
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
