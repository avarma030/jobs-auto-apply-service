from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.routers import applications, auth, jobs, profile, runs, settings, stats
from src.config import settings as app_settings
from src.database.db import Database
from src.database.models import Base
from src.services.run_dispatcher import dispatch_scrape_run
from src.services.saved_search_scheduler import SavedSearchScheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: initialise DB (create tables)
    db = Database(app_settings.database_url)
    await db.init()
    app.state.db = db
    scheduler = SavedSearchScheduler(db, dispatch_scrape_run)
    scheduler.start()
    app.state.saved_search_scheduler = scheduler
    yield
    # Shutdown
    await scheduler.stop()
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

app.include_router(auth.router)
app.include_router(jobs.router)
app.include_router(applications.router)
app.include_router(profile.router)
app.include_router(settings.router)
app.include_router(runs.router)
app.include_router(stats.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
