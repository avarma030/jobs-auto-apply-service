from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select

import src.api.routers.jobs as jobs_router
import src.api.routers.runs as runs_router
from src.database.db import Database
from src.database.models import JobRecord, ScrapeRun, User
from src.models import ApplicationStatus, Job


def make_job(**overrides) -> Job:
    base = dict(
        title="AI Engineer",
        company="Example Corp",
        location="Berlin",
        description="Build AI systems.",
        url="https://www.linkedin.com/jobs/view/1234567890/",
        source_board="linkedin",
        external_id="1234567890",
        easy_apply=True,
        scraped_at=datetime.utcnow(),
        application_status=ApplicationStatus.PENDING,
    )
    base.update(overrides)
    return Job(**base)


@pytest.mark.asyncio
async def test_current_run_pending_jobs_include_rescraped_existing_job(tmp_path):
    db_path = tmp_path / "jobs.db"
    db = Database(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    await db.init()

    async with db.session_factory() as session:
        session.add(User(id=1, email="user@example.com", hashed_password="secret"))
        session.add_all(
            [
                ScrapeRun(id="run-old", user_id=1, status="done"),
                ScrapeRun(id="run-new", user_id=1, status="running"),
            ]
        )
        await session.commit()

    job = make_job()
    await db.upsert_job(job, user_id=1, scrape_run_id="run-old")
    await db.upsert_job(job, user_id=1, scrape_run_id="run-new")

    pending = await db.get_pending_jobs(limit=10, user_id=1, scrape_run_id="run-new")

    assert len(pending) == 1
    assert pending[0].url == job.url

    await db.close()


@pytest.mark.asyncio
async def test_run_summary_helpers_count_linked_jobs_without_overwriting_legacy_run_ids(tmp_path):
    db_path = tmp_path / "jobs.db"
    db = Database(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    await db.init()

    async with db.session_factory() as session:
        session.add(User(id=1, email="user@example.com", hashed_password="secret"))
        session.add_all(
            [
                ScrapeRun(id="run-old", user_id=1, status="done"),
                ScrapeRun(id="run-new", user_id=1, status="running"),
            ]
        )
        await session.commit()

    job = make_job()
    persisted = await db.upsert_job(job, user_id=1, scrape_run_id="run-old")
    await db.upsert_job(job, user_id=1, scrape_run_id="run-new")

    async with db.session_factory() as session:
        refreshed = (
            await session.execute(select(JobRecord).where(JobRecord.id == persisted.id))
        ).scalar_one()
        assert refreshed.scrape_run_id == "run-old"

        run_summaries = await runs_router._job_status_counts_for_runs(
            session,
            1,
            ["run-old", "run-new"],
        )
        saved_search_summaries = await jobs_router._job_status_counts_for_runs(
            session,
            1,
            ["run-old", "run-new"],
        )

    assert run_summaries["run-old"].pending == 1
    assert run_summaries["run-new"].pending == 1
    assert saved_search_summaries["run-old"]["pending"] == 1
    assert saved_search_summaries["run-new"]["pending"] == 1

    await db.close()
