from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api import deps as api_deps
from src.api.routers import applications, auth, jobs, profile, runs, settings, stats
from src.config import settings as app_settings
from src.database.db import Database
from src.services.local_run_worker import LocalRunWorker
from src.services.run_dispatcher import dispatch_scrape_run
from src.services.saved_search_scheduler import SavedSearchScheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database(app_settings.database_url)
    await db.init()
    app.state.db = db
    api_deps._db = db

    local_worker: LocalRunWorker | None = None
    if not app_settings.use_task_queue:
        local_worker = LocalRunWorker(db)
        local_worker.start()
    app.state.local_run_worker = local_worker

    async def _dispatch_run(run_id: str, user_id: int, req) -> None:
        await dispatch_scrape_run(
            run_id,
            user_id,
            req,
            db=db,
            local_worker=local_worker,
        )

    scheduler = SavedSearchScheduler(db, _dispatch_run)
    scheduler.start()
    app.state.saved_search_scheduler = scheduler
    try:
        yield
    finally:
        await scheduler.stop()
        if local_worker is not None:
            await local_worker.stop()
        api_deps._db = None
        await db.close()


app = FastAPI(
    title="Jobs Auto-Apply API",
    version="1.0.0",
    description="Automated job scraping and application service",
    lifespan=lifespan,
)

_cors_origins_raw = os.getenv("CORS_ORIGINS", "*")
_cors_origins = (
    ["*"]
    if _cors_origins_raw == "*"
    else [o.strip() for o in _cors_origins_raw.split(",")]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=(_cors_origins != ["*"]),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health/queue")
async def queue_health():
    db = api_deps.get_database()
    return {
        "mode": "celery" if app_settings.use_task_queue else "local",
        "counts": await db.get_queue_health(),
    }


@app.get("/health/schema")
async def schema_health():
    db = api_deps.get_database()
    return await db.schema_status()


app.include_router(auth.router)
app.include_router(jobs.router)
app.include_router(applications.router)
app.include_router(profile.router)
app.include_router(settings.router)
app.include_router(stats.router)
app.include_router(runs.router)
