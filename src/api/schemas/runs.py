from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class RunResponse(BaseModel):
    id: str
    status: str
    boards: Optional[str] = None
    keywords: Optional[str] = None
    location: Optional[str] = None
    started_at: datetime
    finished_at: Optional[datetime] = None
    jobs_found: int = 0
    jobs_applied: int = 0
    error_message: Optional[str] = None

    model_config = {"from_attributes": True}
