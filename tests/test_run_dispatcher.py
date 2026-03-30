from __future__ import annotations

import pytest

from src.api.schemas.jobs import ScrapeRequest
from src.config import settings
from src.database.db import Database
from src.services.run_dispatcher import dispatch_scrape_run


class FakeLocalWorker:
    def __init__(self) -> None:
        self.notify_calls = 0

    def notify(self) -> None:
        self.notify_calls += 1


@pytest.mark.asyncio
async def test_run_execution_enqueue_claim_and_health(tmp_path):
    db_path = tmp_path / "jobs.db"
    db = Database(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    await db.init()

    assert await db.enqueue_run_execution(
        "run-1",
        user_id=7,
        request_payload={"keywords": ["ai engineer"], "boards": ["linkedin"]},
    )
    assert not await db.enqueue_run_execution(
        "run-1",
        user_id=7,
        request_payload={"keywords": ["ai engineer"], "boards": ["linkedin"]},
    )

    assert await db.mark_run_dispatched("run-1")
    claimed = await db.claim_run_execution("run-1", worker_id="worker:test")

    assert claimed is not None
    assert claimed["run_id"] == "run-1"
    assert claimed["user_id"] == 7
    assert claimed["request_payload"]["keywords"] == ["ai engineer"]
    assert await db.claim_run_execution("run-1", worker_id="worker:test-2") is None

    execution = await db.get_run_execution("run-1")
    assert execution is not None
    assert execution.state == "running"
    assert execution.dispatch_attempts == 1
    assert execution.execution_attempts == 1

    counts = await db.get_queue_health()
    assert counts["running"] == 1
    assert counts["queued"] == 0

    await db.close()


@pytest.mark.asyncio
async def test_request_run_cancellation_cancels_queued_execution(tmp_path):
    db_path = tmp_path / "jobs.db"
    db = Database(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    await db.init()

    await db.enqueue_run_execution(
        "run-cancel",
        user_id=11,
        request_payload={"keywords": ["project manager"], "boards": ["linkedin"]},
    )

    state = await db.request_run_cancellation("run-cancel", user_id=11)

    assert state == "cancelled"
    assert await db.is_run_cancellation_requested("run-cancel") is True
    assert await db.claim_run_execution("run-cancel", worker_id="worker:test") is None

    execution = await db.get_run_execution("run-cancel")
    assert execution is not None
    assert execution.state == "cancelled"

    await db.close()


@pytest.mark.asyncio
async def test_dispatch_scrape_run_enqueues_once_and_notifies_local_worker(tmp_path, monkeypatch):
    db_path = tmp_path / "jobs.db"
    db = Database(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    await db.init()

    worker = FakeLocalWorker()
    req = ScrapeRequest(keywords=["data scientist"], boards=["linkedin"])
    monkeypatch.setattr(settings, "use_task_queue", False)

    await dispatch_scrape_run("run-dispatch", 5, req, db=db, local_worker=worker)
    await dispatch_scrape_run("run-dispatch", 5, req, db=db, local_worker=worker)

    execution = await db.get_run_execution("run-dispatch")
    assert execution is not None
    assert execution.user_id == 5
    assert execution.state == "queued"
    assert worker.notify_calls == 1

    await db.close()
