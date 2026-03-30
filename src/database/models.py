from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    """Application user account."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(256), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class UserProfile(Base):
    """Serialised UserProfile JSON blob per user."""

    __tablename__ = "user_profiles"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    profile_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class UserSettings(Base):
    """Per-user settings overrides stored as JSON."""

    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    settings_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class ScrapeRun(Base):
    """A single scrape+apply run triggered by a user."""

    __tablename__ = "scrape_runs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", index=True
    )  # pending | running | done | failed | stopped
    boards: Mapped[str | None] = mapped_column(String(256), nullable=True)
    keywords: Mapped[str | None] = mapped_column(String(512), nullable=True)
    location: Mapped[str | None] = mapped_column(String(256), nullable=True)
    trigger_type: Mapped[str | None] = mapped_column(String(32), nullable=True, default="manual")
    search_criteria_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    saved_search_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    jobs_found: Mapped[int] = mapped_column(Integer, default=0)
    jobs_applied: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class RunEventRecord(Base):
    """Append-only event log for a scrape/apply run."""

    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("scrape_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True, default="progress")
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    jobs_found: Mapped[int | None] = mapped_column(Integer, nullable=True)
    jobs_applied: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class RunExecutionRecord(Base):
    """Durable queue/worker execution metadata for a scrape run."""

    __tablename__ = "run_executions"

    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("scrape_runs.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    request_payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    dispatch_attempts: Mapped[int] = mapped_column(Integer, default=0)
    execution_attempts: Mapped[int] = mapped_column(Integer, default=0)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class RunJobRecord(Base):
    """Association table recording which jobs were discovered in each run."""

    __tablename__ = "run_jobs"

    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("scrape_runs.id", ondelete="CASCADE"), primary_key=True
    )
    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class JobRecord(Base):
    """Persisted job listing."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    scrape_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("scrape_runs.id", ondelete="SET NULL"), nullable=True
    )
    external_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    source_board: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)

    title: Mapped[str] = mapped_column(String(512), nullable=False)
    company: Mapped[str] = mapped_column(String(256), nullable=False)
    location: Mapped[str | None] = mapped_column(String(256), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    job_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    work_mode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    experience_level: Mapped[str | None] = mapped_column(String(64), nullable=True)

    salary_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)

    skills: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON array
    easy_apply: Mapped[bool] = mapped_column(Boolean, default=False)

    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    scraped_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # AI pipeline
    match_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    ats_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    ats_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tailored_resume_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    cover_letter_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    application_status: Mapped[str] = mapped_column(String(64), default="pending", index=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class ApplicationRecord(Base):
    """Log of every application attempt."""

    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    job_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmation_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)


class SemanticCacheRecord(Base):
    """Cached semantic extractions and match decisions."""

    __tablename__ = "semantic_cache"

    key: Mapped[str] = mapped_column(String(191), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class BoardAccountCredentialRecord(Base):
    """Encrypted job-board credentials stored outside profile JSON."""

    __tablename__ = "board_account_credentials"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    board: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str | None] = mapped_column(String(256), nullable=True)
    encrypted_secret_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class BoardSessionRecord(Base):
    """User-scoped board session metadata and auth state."""

    __tablename__ = "board_sessions"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "board",
            "account_key",
            "session_kind",
            name="uq_board_session_identity_kind",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    board: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    account_key: Mapped[str] = mapped_column(String(128), nullable=False)
    account_username: Mapped[str | None] = mapped_column(String(256), nullable=True)
    session_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    auth_state: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    challenge_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cookie_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    session_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
