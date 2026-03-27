from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from src.utils.time import utcnow_naive


class Base(DeclarativeBase):
    pass


class JobRecord(Base):
    """Persisted job listing."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    source_board: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)

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
    scraped_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)

    application_status: Mapped[str] = mapped_column(String(64), default="pending", index=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class ApplicationRecord(Base):
    """Log of every application attempt."""

    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmation_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
