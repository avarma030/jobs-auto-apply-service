from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace

import pytest
from sqlalchemy import select

sys.modules.setdefault("openpyxl", types.SimpleNamespace())

import src.api.routers.jobs as jobs_router
from src.api.schemas.jobs import ScrapeRequest
from src.database.db import Database
from src.database.models import RunEventRecord, ScrapeRun, User, UserSettings


@pytest.mark.asyncio
async def test_trigger_scrape_persists_run_before_run_event_with_saved_search(tmp_path, monkeypatch):
    db_path = tmp_path / "jobs.db"
    db = Database(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    await db.init()

    async with db.session_factory() as session:
        user = User(email="route@example.com", hashed_password="secret")
        session.add(user)
        await session.commit()
        await session.refresh(user)

        dispatched: list[tuple[str, int, ScrapeRequest]] = []

        async def fake_dispatch(run_id: str, user_id: int, req: ScrapeRequest, **kwargs) -> None:
            dispatched.append((run_id, user_id, req))

        monkeypatch.setattr(jobs_router, "dispatch_scrape_run", fake_dispatch)

        body = ScrapeRequest(
            keywords=["ai engineer"],
            location="Frankfurt",
            boards=["linkedin"],
            easy_apply_only=True,
            max_age_hours=3,
            save_search=True,
            saved_search_enabled=True,
            saved_search_interval_hours=3,
        )
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(local_run_worker=None)))

        response = await jobs_router.trigger_scrape(
            body,
            request=request,
            db=db,
            session=session,
            current_user=user,
        )

        assert response["run_id"]
        assert dispatched and dispatched[0][0] == response["run_id"]

    async with db.session_factory() as session:
        run = (
            await session.execute(select(ScrapeRun).where(ScrapeRun.id == response["run_id"]))
        ).scalar_one()
        assert run.user_id == 1
        assert run.status == "pending"

        events = list(
            (
                await session.execute(
                    select(RunEventRecord).where(RunEventRecord.run_id == response["run_id"])
                )
            ).scalars().all()
        )
        assert len(events) == 1
        assert events[0].message == "Run queued"
        assert events[0].status == "pending"

        settings = (
            await session.execute(select(UserSettings).where(UserSettings.user_id == user.id))
        ).scalar_one()
        saved_search = json.loads(settings.settings_json)["saved_search"]
        assert saved_search["last_run_id"] == response["run_id"]

    await db.close()
