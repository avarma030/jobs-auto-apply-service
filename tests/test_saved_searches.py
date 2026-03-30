from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from src.api.schemas.jobs import SavedSearchConfig, ScrapeRequest, SearchCriteria
from src.database.db import Database
from src.database.models import ScrapeRun, User, UserSettings
from src.services.saved_search_scheduler import SavedSearchScheduler
from src.services.saved_searches import (
    saved_search_state,
    saved_search_key,
    scrape_request_from_saved_search,
    serialized_search_criteria,
    update_saved_search_config,
)


def test_update_saved_search_config_persists_manual_run_and_hour_filter():
    started_at = datetime(2026, 3, 30, 9, 0, tzinfo=timezone.utc)
    req = ScrapeRequest(
        keywords=["ai engineer"],
        location="London",
        boards=["linkedin"],
        max_age_days=7,
        max_age_hours=3,
        max_jobs=10,
        saved_search_enabled=True,
        saved_search_interval_hours=3,
    )

    updated = update_saved_search_config({}, req, run_started_at=started_at, run_id="run-123")

    assert updated.enabled is True
    assert updated.interval_hours == 3
    assert updated.criteria is not None
    assert updated.criteria.max_age_hours == 3
    assert updated.criteria.location == "London"
    assert updated.last_triggered_at == started_at
    assert updated.last_run_id == "run-123"


def test_saved_search_state_computes_next_trigger():
    raw = SavedSearchConfig(
        enabled=True,
        interval_hours=1,
        criteria=SearchCriteria(keywords=["project manager"], location="Dublin"),
        last_triggered_at=datetime(2026, 3, 30, 10, 0, tzinfo=timezone.utc),
        last_run_id="run-abc",
    ).model_dump(mode="json", exclude_none=True)

    state = saved_search_state(raw)

    assert state.next_trigger_at == datetime(2026, 3, 30, 11, 0, tzinfo=timezone.utc)


def test_saved_search_state_exposes_run_history():
    raw = SavedSearchConfig(
        enabled=True,
        interval_hours=3,
        criteria=SearchCriteria(keywords=["project manager"], location="Dublin"),
    ).model_dump(mode="json", exclude_none=True)

    state = saved_search_state(
        raw,
        run_count=2,
        runs=[],
    )

    assert state.run_count == 2
    assert state.runs == []


def test_saved_search_key_stays_stable_for_equivalent_criteria():
    criteria = SearchCriteria(
        keywords=["ai engineer"],
        location="Frankfurt",
        boards=["linkedin"],
        max_age_days=7,
        max_age_hours=3,
    )

    payload = json.loads(serialized_search_criteria(criteria))

    assert payload["keywords"] == ["ai engineer"]
    assert payload["location"] == "Frankfurt"
    assert payload["max_age_hours"] == 3
    assert "max_age_days" not in payload
    assert saved_search_key(criteria) == saved_search_key(criteria.model_copy())


def test_scrape_request_from_saved_search_round_trips_criteria():
    config = SavedSearchConfig(
        enabled=True,
        interval_hours=3,
        criteria=SearchCriteria(
            keywords=["machine learning engineer"],
            location="Chicago",
            boards=["linkedin"],
            max_age_hours=1,
            tailor_documents=True,
            min_match_score=80,
        ),
    )

    req = scrape_request_from_saved_search(config)

    assert req is not None
    assert req.max_age_hours == 1
    assert req.tailor_documents is True
    assert req.min_match_score == 80


@pytest.mark.asyncio
async def test_saved_search_scheduler_triggers_due_search(tmp_path):
    db_path = tmp_path / "jobs.db"
    db = Database(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    await db.init()

    async with db.session_factory() as session:
        user = User(email="rucha@example.com", hashed_password="secret")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        session.add(
            UserSettings(
                user_id=user.id,
                settings_json=json.dumps(
                    {
                        "saved_search": SavedSearchConfig(
                            enabled=True,
                            interval_hours=3,
                            criteria=SearchCriteria(
                                keywords=["it project manager"],
                                location="Dublin",
                                boards=["linkedin"],
                                easy_apply_only=True,
                                max_age_hours=3,
                            ),
                            last_triggered_at=datetime.now(timezone.utc) - timedelta(hours=4),
                        ).model_dump(mode="json", exclude_none=True)
                    }
                ),
            )
        )
        await session.commit()

    launched: list[tuple[str, int, ScrapeRequest]] = []

    async def fake_run_scrape(run_id: str, user_id: int, req: ScrapeRequest) -> None:
        launched.append((run_id, user_id, req))

    scheduler = SavedSearchScheduler(db, fake_run_scrape)
    count = await scheduler.run_pending_once()
    await asyncio.sleep(0)

    assert count == 1
    assert len(launched) == 1
    assert launched[0][2].keywords == ["it project manager"]
    assert launched[0][2].max_age_hours == 3

    async with db.session_factory() as session:
        runs = list((await session.execute(select(ScrapeRun))).scalars().all())
        assert len(runs) == 1
        assert runs[0].status == "pending"
        assert runs[0].trigger_type == "saved_search"
        assert runs[0].search_criteria_json == serialized_search_criteria(
            SearchCriteria(
                keywords=["it project manager"],
                location="Dublin",
                boards=["linkedin"],
                easy_apply_only=True,
                max_age_hours=3,
            )
        )
        assert runs[0].saved_search_key == saved_search_key(
            SearchCriteria(
                keywords=["it project manager"],
                location="Dublin",
                boards=["linkedin"],
                easy_apply_only=True,
                max_age_hours=3,
            )
        )
        settings_row = (
            await session.execute(select(UserSettings).where(UserSettings.user_id == launched[0][1]))
        ).scalar_one()
        saved = json.loads(settings_row.settings_json)["saved_search"]
        assert saved["last_run_id"] == runs[0].id

    await db.close()


@pytest.mark.asyncio
async def test_saved_search_scheduler_skips_users_with_active_runs(tmp_path):
    db_path = tmp_path / "jobs.db"
    db = Database(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    await db.init()

    async with db.session_factory() as session:
        user = User(email="rucha@example.com", hashed_password="secret")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        session.add(
            UserSettings(
                user_id=user.id,
                settings_json=json.dumps(
                    {
                        "saved_search": SavedSearchConfig(
                            enabled=True,
                            interval_hours=3,
                            criteria=SearchCriteria(
                                keywords=["it project manager"],
                                location="Dublin",
                                boards=["linkedin"],
                            ),
                            last_triggered_at=datetime.now(timezone.utc) - timedelta(hours=4),
                        ).model_dump(mode="json", exclude_none=True)
                    }
                ),
            )
        )
        session.add(
            ScrapeRun(
                user_id=user.id,
                status="running",
                boards="linkedin",
                keywords="it project manager",
                location="Dublin",
                started_at=datetime.utcnow(),
            )
        )
        await session.commit()

    launched: list[tuple[str, int, ScrapeRequest]] = []

    async def fake_run_scrape(run_id: str, user_id: int, req: ScrapeRequest) -> None:
        launched.append((run_id, user_id, req))

    scheduler = SavedSearchScheduler(db, fake_run_scrape)
    count = await scheduler.run_pending_once()
    await asyncio.sleep(0)

    assert count == 0
    assert launched == []

    await db.close()
