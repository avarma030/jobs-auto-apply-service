from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class BoardCapabilityResponse(BaseModel):
    slug: str
    label: str
    scrape_supported: bool
    apply_supported: bool
    production_ready: bool
    status: str


class SettingsResponse(BaseModel):
    auto_apply: bool = True
    max_applications_per_day: int = 50
    easy_apply_only: bool = False
    headless_browser: bool = True
    dry_run: bool = False
    enabled_boards: list[str] = []
    preferred_work_modes: list[str] = []
    blacklisted_companies: list[str] = []
    request_delay_seconds: float = 2.0
    custom_answers: dict[str, str] = {}
    supported_boards: list[str] = []
    board_capabilities: list[BoardCapabilityResponse] = []


class SettingsUpdate(BaseModel):
    auto_apply: Optional[bool] = None
    max_applications_per_day: Optional[int] = None
    easy_apply_only: Optional[bool] = None
    headless_browser: Optional[bool] = None
    dry_run: Optional[bool] = None
    enabled_boards: Optional[list[str]] = None
    preferred_work_modes: Optional[list[str]] = None
    blacklisted_companies: Optional[list[str]] = None
    request_delay_seconds: Optional[float] = None
    custom_answers: Optional[dict[str, str]] = None
