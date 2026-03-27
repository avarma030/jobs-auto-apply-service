from __future__ import annotations

from pydantic import BaseModel


class StatsResponse(BaseModel):
    total_scraped: int = 0
    total_applied: int = 0
    total_skipped: int = 0
    total_failed: int = 0
    total_interviewing: int = 0
    total_offered: int = 0
    total_rejected: int = 0
    success_rate: float = 0.0  # applied / (applied + failed) * 100
    this_week_applied: int = 0
    by_board: dict[str, int] = {}
