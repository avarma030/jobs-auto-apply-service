from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.routers import applications, auth, jobs, profile, runs, settings, stats
from src.config import settings as app_settings
from src.database.db import Database
from src.database.models import Base


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: initialise DB (create tables)
    db = Database(app_settings.database_url)
    await db.init()
    app.state.db = db
    yield
    # Shutdown
    await db.close()


app = FastAPI(
    title="Jobs Auto-Apply API",
    version="1.0.0",
    description="Automated job scraping and application service",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
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
