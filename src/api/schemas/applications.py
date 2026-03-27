from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class ApplicationResponse(BaseModel):
    id: int
    job_id: int
    attempted_at: datetime
    status: str
    confirmation_id: Optional[str] = None
    message: Optional[str] = None

    model_config = {"from_attributes": True}


class ApplicationUpdate(BaseModel):
    status: str
    notes: Optional[str] = None


class ApplicationsPage(BaseModel):
    items: list[ApplicationResponse]
    total: int
    page: int
    page_size: int
