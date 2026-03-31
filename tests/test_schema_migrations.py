from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, inspect, select

from src.database.db import Database
from src.database.models import ApplicationRecord, JobRecord, RunJobRecord
from src.database.schema import SchemaMismatchError, migrate_database_to_head
from src.models import ApplicationStatus, Job


def _legacy_database_url(path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def _build_legacy_schema(path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email VARCHAR(256) NOT NULL UNIQUE,
            hashed_password VARCHAR(256) NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        );

        CREATE TABLE scrape_runs (
            id VARCHAR(36) PRIMARY KEY,
            user_id INTEGER NOT NULL,
            status VARCHAR(32) NOT NULL,
            boards VARCHAR(256),
            keywords VARCHAR(512),
            location VARCHAR(256),
            trigger_type VARCHAR(32),
            search_criteria_json TEXT,
            saved_search_key VARCHAR(64),
            started_at DATETIME NOT NULL,
            finished_at DATETIME,
            jobs_found INTEGER NOT NULL DEFAULT 0,
            jobs_applied INTEGER NOT NULL DEFAULT 0,
            error_message TEXT
        );

        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            scrape_run_id VARCHAR(36),
            external_id VARCHAR(256),
            source_board VARCHAR(64) NOT NULL,
            url TEXT NOT NULL,
            title VARCHAR(512) NOT NULL,
            company VARCHAR(256) NOT NULL,
            location VARCHAR(256),
            description TEXT,
            job_type VARCHAR(64),
            work_mode VARCHAR(64),
            experience_level VARCHAR(64),
            salary_min FLOAT,
            salary_max FLOAT,
            salary_currency VARCHAR(8),
            skills TEXT,
            easy_apply BOOLEAN DEFAULT 0,
            posted_at DATETIME,
            scraped_at DATETIME NOT NULL,
            match_score FLOAT,
            ats_score FLOAT,
            ats_type VARCHAR(64),
            tailored_resume_path VARCHAR(512),
            cover_letter_path VARCHAR(512),
            application_status VARCHAR(64) DEFAULT 'pending',
            applied_at DATETIME,
            notes TEXT
        );

        CREATE TABLE run_jobs (
            run_id VARCHAR(36) NOT NULL,
            job_id INTEGER NOT NULL,
            discovered_at DATETIME NOT NULL,
            PRIMARY KEY (run_id, job_id)
        );

        CREATE TABLE applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            job_id INTEGER NOT NULL,
            attempted_at DATETIME NOT NULL,
            status VARCHAR(64) NOT NULL,
            confirmation_id VARCHAR(256),
            message TEXT
        );
        """
    )

    now = datetime.utcnow()
    older = now - timedelta(days=2)
    newer = now - timedelta(hours=1)

    conn.execute(
        "INSERT INTO users (email, hashed_password, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("legacy@example.com", "secret", older.isoformat(), older.isoformat()),
    )
    conn.execute(
        """
        INSERT INTO scrape_runs
        (id, user_id, status, boards, keywords, location, trigger_type, search_criteria_json, saved_search_key, started_at, finished_at, jobs_found, jobs_applied, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "run-old",
            1,
            "done",
            "linkedin",
            "ai engineer",
            "Dublin",
            "manual",
            None,
            None,
            older.isoformat(),
            older.isoformat(),
            1,
            1,
            None,
        ),
    )
    conn.execute(
        """
        INSERT INTO scrape_runs
        (id, user_id, status, boards, keywords, location, trigger_type, search_criteria_json, saved_search_key, started_at, finished_at, jobs_found, jobs_applied, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "run-new",
            1,
            "pending",
            "linkedin",
            "ai engineer",
            "Dublin",
            "manual",
            None,
            None,
            newer.isoformat(),
            None,
            1,
            0,
            None,
        ),
    )

    conn.execute(
        """
        INSERT INTO jobs
        (id, user_id, scrape_run_id, external_id, source_board, url, title, company, location, description, easy_apply, scraped_at, application_status, applied_at, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            1,
            "run-new",
            "123",
            "linkedin",
            "https://www.linkedin.com/jobs/view/123?tracking=abc",
            "AI Engineer",
            "Example Corp",
            "Dublin",
            "Duplicate pending row",
            1,
            newer.isoformat(),
            "pending",
            None,
            "new duplicate",
        ),
    )
    conn.execute(
        """
        INSERT INTO jobs
        (id, user_id, scrape_run_id, external_id, source_board, url, title, company, location, description, easy_apply, scraped_at, application_status, applied_at, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            2,
            1,
            "run-old",
            "123",
            "linkedin",
            "https://www.linkedin.com/jobs/view/123/",
            "AI Engineer",
            "Example Corp",
            "Dublin",
            "Canonical applied row",
            1,
            older.isoformat(),
            "applied",
            older.isoformat(),
            "already applied",
        ),
    )
    conn.execute(
        "INSERT INTO run_jobs (run_id, job_id, discovered_at) VALUES (?, ?, ?)",
        ("run-new", 1, newer.isoformat()),
    )
    conn.execute(
        "INSERT INTO run_jobs (run_id, job_id, discovered_at) VALUES (?, ?, ?)",
        ("run-old", 2, older.isoformat()),
    )
    conn.execute(
        """
        INSERT INTO applications (user_id, job_id, attempted_at, status, confirmation_id, message)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (1, 1, newer.isoformat(), "failed", None, "legacy duplicate application"),
    )
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_database_init_bootstraps_empty_db_to_head(tmp_path):
    db_path = tmp_path / "empty.db"
    db = Database(_legacy_database_url(db_path))
    await db.init()

    status = await db.schema_status()

    assert status["at_head"] is True
    assert status["current_revision"] == status["head_revision"]
    assert status["has_legacy_schema"] is False

    await db.close()


@pytest.mark.asyncio
async def test_database_init_fails_fast_on_unversioned_legacy_schema(tmp_path):
    db_path = tmp_path / "legacy.db"
    _build_legacy_schema(db_path)

    db = Database(_legacy_database_url(db_path))
    with pytest.raises(SchemaMismatchError, match="alembic upgrade head"):
        await db.init()
    await db.close()


@pytest.mark.asyncio
async def test_legacy_database_migrates_forward_and_dedupes_jobs(tmp_path):
    db_path = tmp_path / "legacy-upgrade.db"
    _build_legacy_schema(db_path)

    await asyncio.to_thread(migrate_database_to_head, _legacy_database_url(db_path))

    db = Database(_legacy_database_url(db_path))
    await db.init()

    async with db.session_factory() as session:
        jobs = list((await session.execute(select(JobRecord).order_by(JobRecord.id.asc()))).scalars().all())
        assert len(jobs) == 1
        assert jobs[0].id == 2
        assert jobs[0].application_status == ApplicationStatus.APPLIED
        assert jobs[0].normalized_url == "https://www.linkedin.com/jobs/view/123"

        application = (await session.execute(select(ApplicationRecord))).scalar_one()
        assert application.job_id == 2

        run_links = list(
            (
                await session.execute(
                    select(RunJobRecord).order_by(RunJobRecord.run_id.asc())
                )
            ).scalars().all()
        )
        assert {(link.run_id, link.job_id) for link in run_links} == {
            ("run-new", 2),
            ("run-old", 2),
        }

        async with db.engine.connect() as conn:
            def _inspect_schema(sync_conn):
                inspector = inspect(sync_conn)
                return (
                    set(inspector.get_table_names()),
                    inspector.get_foreign_keys("applications"),
                    {idx["name"] for idx in inspector.get_indexes("jobs")},
                )

            table_names, application_fks, job_indexes = await conn.run_sync(_inspect_schema)
        assert "run_events" in table_names
        assert "run_executions" in table_names
        assert any(
            fk.get("referred_table") == "jobs" and "job_id" in (fk.get("constrained_columns") or [])
            for fk in application_fks
        )
        assert "uq_jobs_user_normalized_url" in job_indexes
        assert "uq_jobs_user_source_external_id" in job_indexes

    await db.close()


@pytest.mark.asyncio
async def test_upsert_job_uses_normalized_url_identity(tmp_path):
    db_path = tmp_path / "identity.db"
    db = Database(_legacy_database_url(db_path))
    await db.init()

    first = Job(
        title="AI Engineer",
        company="Example Corp",
        url="https://www.linkedin.com/jobs/view/999?trk=feed",
        source_board="linkedin",
        external_id="999",
    )
    second = Job(
        title="AI Engineer",
        company="Example Corp",
        url="https://www.linkedin.com/jobs/view/999/",
        source_board="linkedin",
        external_id="999",
    )

    row_one = await db.upsert_job(first, user_id=4, scrape_run_id=None)
    row_two = await db.upsert_job(second, user_id=4, scrape_run_id=None)

    assert row_one.id == row_two.id

    async with db.session_factory() as session:
        count = (await session.execute(select(func.count()).select_from(JobRecord))).scalar_one()
        assert count == 1
        persisted = (await session.execute(select(JobRecord))).scalar_one()
        assert persisted.normalized_url == "https://www.linkedin.com/jobs/view/999"

    await db.close()
