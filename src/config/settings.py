from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application-wide settings loaded from environment variables / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------
    app_name: str = "jobs-auto-apply-service"
    secret_key: str = "changeme-replace-with-a-long-random-secret-in-production"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    dry_run: bool = Field(
        default=False,
        description="If True, scrape jobs but do NOT submit any applications",
    )

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./data/jobs.db"

    # ------------------------------------------------------------------
    # User profile
    # ------------------------------------------------------------------
    user_profile_path: Path = Path("data/user_profile.json")
    resume_path: Path = Path("data/resume.pdf")
    cover_letter_template_path: Path | None = None

    # ------------------------------------------------------------------
    # Scraping
    # ------------------------------------------------------------------
    # Comma-separated list of boards to enable; "all" enables everything
    enabled_boards: str = "all"
    scrape_interval_minutes: int = Field(default=60, ge=5)
    max_concurrent_scrapers: int = Field(default=3, ge=1)
    request_delay_seconds: float = Field(default=2.0, ge=0.5)
    use_proxies: bool = False
    proxy_list_path: Path | None = None

    # ------------------------------------------------------------------
    # Applying
    # ------------------------------------------------------------------
    max_applications_per_run: int = Field(default=50, ge=1)
    headless_browser: bool = True
    browser_timeout_seconds: int = 30
    screenshot_on_failure: bool = True
    screenshots_dir: Path = Path("data/screenshots")

    # ------------------------------------------------------------------
    # Redis / Celery (optional — used for task queue mode)
    # ------------------------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"
    use_task_queue: bool = False

    # ------------------------------------------------------------------
    # Notifications (optional)
    # ------------------------------------------------------------------
    notify_email: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None

    slack_webhook_url: str | None = None

    def enabled_board_list(self) -> list[str]:
        if self.enabled_boards.strip().lower() == "all":
            return [
                "linkedin",
                "indeed",
                "glassdoor",
                "ziprecruiter",
                "dice",
                "monster",
                "lever",
                "greenhouse",
                "workday",
            ]
        return [b.strip().lower() for b in self.enabled_boards.split(",") if b.strip()]


# Singleton instance — import this everywhere
settings = Settings()
