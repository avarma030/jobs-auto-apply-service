from __future__ import annotations

from datetime import datetime, timezone


def utcnow_naive() -> datetime:
    """Return a UTC timestamp without tzinfo for legacy storage fields."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
