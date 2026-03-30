from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from src.api.schemas.jobs import SavedSearchConfig, SavedSearchState, ScrapeRequest, SearchCriteria


def search_criteria_from_request(req: ScrapeRequest) -> SearchCriteria:
    payload = req.model_dump(
        exclude={
            "save_search",
            "saved_search_enabled",
            "saved_search_interval_hours",
        },
        exclude_none=True,
    )
    return SearchCriteria(**payload)


def load_saved_search_config(raw: Any) -> SavedSearchConfig:
    if not isinstance(raw, dict):
        return SavedSearchConfig()
    try:
        return SavedSearchConfig.model_validate(raw)
    except Exception:
        return SavedSearchConfig()


def saved_search_state(raw: Any) -> SavedSearchState:
    config = load_saved_search_config(raw)
    next_trigger_at = None
    if config.enabled and config.criteria and config.last_triggered_at:
        next_trigger_at = config.last_triggered_at + timedelta(hours=config.interval_hours)
    return SavedSearchState(**config.model_dump(), next_trigger_at=next_trigger_at)


def update_saved_search_config(
    current_raw: Any,
    req: ScrapeRequest,
    *,
    run_started_at: datetime | None = None,
    run_id: str | None = None,
) -> SavedSearchConfig:
    current = load_saved_search_config(current_raw)
    enabled = current.enabled if req.saved_search_enabled is None else req.saved_search_enabled
    interval_hours = req.saved_search_interval_hours or current.interval_hours
    updated = SavedSearchConfig(
        enabled=enabled,
        interval_hours=interval_hours,
        criteria=search_criteria_from_request(req),
        last_triggered_at=current.last_triggered_at,
        last_run_id=current.last_run_id,
    )
    if updated.enabled and run_started_at is not None:
        updated.last_triggered_at = _as_utc(run_started_at)
        updated.last_run_id = run_id
    return updated


def saved_search_is_due(config: SavedSearchConfig, *, now: datetime | None = None) -> bool:
    if not config.enabled or config.criteria is None:
        return False
    if config.last_triggered_at is None:
        return True
    now_utc = _as_utc(now or datetime.now(timezone.utc))
    return config.last_triggered_at + timedelta(hours=config.interval_hours) <= now_utc


def scrape_request_from_saved_search(config: SavedSearchConfig) -> ScrapeRequest | None:
    if config.criteria is None:
        return None
    return ScrapeRequest(**config.criteria.model_dump(mode="json"))


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
