from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Awaitable, Callable

from loguru import logger
from sqlalchemy import select

from src.database.db import Database
from src.database.models import RunEventRecord, ScrapeRun, UserSettings
from src.services.saved_searches import (
    load_saved_search_config,
    saved_search_is_due,
    saved_search_key,
    scrape_request_from_saved_search,
    serialized_search_criteria,
)

RunScrapeFunc = Callable[[str, int, object], Awaitable[None]]


class SavedSearchScheduler:
    def __init__(
        self,
        db: Database,
        run_scrape: RunScrapeFunc,
        *,
        poll_interval_seconds: int = 60,
    ) -> None:
        self._db = db
        self._run_scrape = run_scrape
        self._poll_interval_seconds = poll_interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def run_pending_once(self) -> int:
        launches: list[tuple[str, int, object]] = []
        now = datetime.now(timezone.utc)

        async with self._db.session_factory() as session:
            settings_rows = list((await session.execute(select(UserSettings))).scalars().all())
            for row in settings_rows:
                config = load_saved_search_config(json.loads(row.settings_json or "{}").get("saved_search"))
                if not saved_search_is_due(config, now=now):
                    continue
                req = scrape_request_from_saved_search(config)
                if req is None:
                    continue
                if await self._user_has_active_run(session, row.user_id):
                    continue

                run = ScrapeRun(
                    id=str(uuid.uuid4()),
                    user_id=row.user_id,
                    status="pending",
                    boards=",".join(req.boards),
                    keywords=",".join(req.keywords),
                    location=req.location,
                    trigger_type="saved_search",
                    search_criteria_json=serialized_search_criteria(config.criteria),
                    saved_search_key=saved_search_key(config.criteria),
                    started_at=now.replace(tzinfo=None),
                )
                session.add(run)
                session.add(
                    RunEventRecord(
                        run_id=run.id,
                        user_id=row.user_id,
                        event_type="status",
                        level="info",
                        message="Run queued by saved search scheduler",
                        status="pending",
                    )
                )

                settings_data = json.loads(row.settings_json or "{}")
                saved_search = settings_data.get("saved_search") or {}
                saved_search["last_triggered_at"] = now.isoformat()
                saved_search["last_run_id"] = run.id
                settings_data["saved_search"] = saved_search
                row.settings_json = json.dumps(settings_data)
                launches.append((run.id, row.user_id, req))

            await session.commit()

        for run_id, user_id, req in launches:
            logger.info(
                f"[SavedSearch] Triggering saved search for user {user_id} "
                f"({', '.join(req.keywords)} @ {req.location or 'anywhere'})"
            )
            asyncio.create_task(self._run_scrape(run_id, user_id, req))

        return len(launches)

    async def _run_loop(self) -> None:
        logger.info("[SavedSearch] Scheduler started")
        try:
            while not self._stopped.is_set():
                try:
                    await self.run_pending_once()
                except Exception as exc:
                    logger.exception(f"[SavedSearch] Scheduler tick failed: {exc}")

                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=self._poll_interval_seconds)
                except asyncio.TimeoutError:
                    continue
        finally:
            logger.info("[SavedSearch] Scheduler stopped")

    @staticmethod
    async def _user_has_active_run(session, user_id: int) -> bool:
        existing = (
            await session.execute(
                select(ScrapeRun.id).where(
                    ScrapeRun.user_id == user_id,
                    ScrapeRun.status.in_(("pending", "running")),
                )
            )
        ).first()
        return existing is not None
