from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass(slots=True)
class SchemaStatus:
    current_revision: str | None
    head_revision: str
    at_head: bool
    is_empty: bool
    has_legacy_schema: bool


class SchemaMismatchError(RuntimeError):
    pass


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def make_alembic_config(database_url: str) -> Config:
    root = _repo_root()
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def get_head_revision(database_url: str) -> str:
    config = make_alembic_config(database_url)
    return ScriptDirectory.from_config(config).get_current_head()


def migrate_database_to_head(database_url: str) -> None:
    command.upgrade(make_alembic_config(database_url), "head")


async def get_schema_status(engine: AsyncEngine, database_url: str) -> SchemaStatus:
    head_revision = get_head_revision(database_url)

    async with engine.connect() as conn:
        def _inspect(sync_conn):
            inspector = inspect(sync_conn)
            tables = set(inspector.get_table_names())
            current_revision = None
            if "alembic_version" in tables:
                current_revision = MigrationContext.configure(sync_conn).get_current_revision()
            return tables, current_revision

        tables, current_revision = await conn.run_sync(_inspect)

    application_tables = {table for table in tables if table != "alembic_version"}
    is_empty = not application_tables
    has_legacy_schema = bool(application_tables) and current_revision is None

    return SchemaStatus(
        current_revision=current_revision,
        head_revision=head_revision,
        at_head=current_revision == head_revision,
        is_empty=is_empty,
        has_legacy_schema=has_legacy_schema,
    )


async def ensure_database_schema(engine: AsyncEngine, database_url: str) -> SchemaStatus:
    status = await get_schema_status(engine, database_url)
    if status.is_empty:
        await asyncio.to_thread(migrate_database_to_head, database_url)
        status = await get_schema_status(engine, database_url)

    if not status.at_head:
        raise SchemaMismatchError(
            "Database schema is not at the expected Alembic revision. "
            f"Current revision: {status.current_revision or 'unversioned legacy schema'}, "
            f"expected: {status.head_revision}. Run `alembic upgrade head` before starting the API."
        )

    return status
