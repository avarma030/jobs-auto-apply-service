"""schema baseline with job identity hardening

Revision ID: 20260331_0001
Revises:
Create Date: 2026-03-31 00:00:00
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import unquote, urlsplit, urlunsplit

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260331_0001"
down_revision = None
branch_labels = None
depends_on = None


def _normalize_job_url(url: str | None) -> str | None:
    if not url:
        return None
    raw = url.strip()
    if not raw:
        return None
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = unquote(parts.path or "").strip() or "/"
    if path != "/":
        path = path.rstrip("/")
    while "//" in path:
        path = path.replace("//", "/")
    return urlunsplit((scheme, netloc, path, "", ""))


def _parse_dt(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if not value:
        return datetime.min
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min


def _status_priority(status: str | None) -> int:
    lowered = (status or "").lower()
    if lowered in {"applied", "interviewed", "offered"}:
        return 3
    if lowered == "pending":
        return 2
    if lowered in {"failed", "skipped"}:
        return 1
    return 0


def _table_names(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _column_names(bind, table_name: str) -> set[str]:
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def _has_fk(bind, table_name: str, constrained_column: str, referred_table: str) -> bool:
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    for foreign_key in inspector.get_foreign_keys(table_name):
        if (
            foreign_key.get("referred_table") == referred_table
            and constrained_column in (foreign_key.get("constrained_columns") or [])
        ):
            return True
    return False


def _create_table_users() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("email", sa.String(length=256), nullable=False, unique=True),
        sa.Column("hashed_password", sa.String(length=256), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_users_email ON users (email)")


def _create_table_user_profiles() -> None:
    op.create_table(
        "user_profiles",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("profile_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def _create_table_user_settings() -> None:
    op.create_table(
        "user_settings",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("settings_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def _create_table_scrape_runs() -> None:
    op.create_table(
        "scrape_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("boards", sa.String(length=256), nullable=True),
        sa.Column("keywords", sa.String(length=512), nullable=True),
        sa.Column("location", sa.String(length=256), nullable=True),
        sa.Column("trigger_type", sa.String(length=32), nullable=True, server_default="manual"),
        sa.Column("search_criteria_json", sa.Text(), nullable=True),
        sa.Column("saved_search_key", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("jobs_found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("jobs_applied", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_scrape_runs_user_id ON scrape_runs (user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_scrape_runs_status ON scrape_runs (status)")


def _create_table_run_events() -> None:
    op.create_table(
        "run_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(length=36), sa.ForeignKey("scrape_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False, server_default="progress"),
        sa.Column("level", sa.String(length=16), nullable=False, server_default="info"),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("jobs_found", sa.Integer(), nullable=True),
        sa.Column("jobs_applied", sa.Integer(), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_run_events_run_id ON run_events (run_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_run_events_user_id ON run_events (user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_run_events_event_type ON run_events (event_type)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_run_events_created_at ON run_events (created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_run_events_run_id_id ON run_events (run_id, id)")


def _create_table_run_executions() -> None:
    op.create_table(
        "run_executions",
        sa.Column("run_id", sa.String(length=36), sa.ForeignKey("scrape_runs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("request_payload_json", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("worker_id", sa.String(length=128), nullable=True),
        sa.Column("dispatch_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("execution_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dispatched_at", sa.DateTime(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_run_executions_user_id ON run_executions (user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_run_executions_state ON run_executions (state)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_run_executions_worker_id ON run_executions (worker_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_run_executions_created_at ON run_executions (created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_run_executions_state_created_at ON run_executions (state, created_at)")


def _create_table_jobs() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("scrape_run_id", sa.String(length=36), sa.ForeignKey("scrape_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("external_id", sa.String(length=256), nullable=True),
        sa.Column("source_board", sa.String(length=64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("normalized_url", sa.Text(), nullable=True),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("company", sa.String(length=256), nullable=False),
        sa.Column("location", sa.String(length=256), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("job_type", sa.String(length=64), nullable=True),
        sa.Column("work_mode", sa.String(length=64), nullable=True),
        sa.Column("experience_level", sa.String(length=64), nullable=True),
        sa.Column("salary_min", sa.Float(), nullable=True),
        sa.Column("salary_max", sa.Float(), nullable=True),
        sa.Column("salary_currency", sa.String(length=8), nullable=True),
        sa.Column("skills", sa.Text(), nullable=True),
        sa.Column("easy_apply", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("posted_at", sa.DateTime(), nullable=True),
        sa.Column("scraped_at", sa.DateTime(), nullable=False),
        sa.Column("match_score", sa.Float(), nullable=True),
        sa.Column("ats_score", sa.Float(), nullable=True),
        sa.Column("ats_type", sa.String(length=64), nullable=True),
        sa.Column("tailored_resume_path", sa.String(length=512), nullable=True),
        sa.Column("cover_letter_path", sa.String(length=512), nullable=True),
        sa.Column("application_status", sa.String(length=64), nullable=False, server_default="pending"),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_jobs_user_id ON jobs (user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_jobs_external_id ON jobs (external_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_jobs_source_board ON jobs (source_board)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_jobs_application_status ON jobs (application_status)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_jobs_user_status_scraped_at "
        "ON jobs (user_id, application_status, scraped_at)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_user_normalized_url "
        "ON jobs (user_id, normalized_url)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_user_source_external_id "
        "ON jobs (user_id, source_board, external_id) "
        "WHERE external_id IS NOT NULL"
    )


def _create_table_run_jobs() -> None:
    op.create_table(
        "run_jobs",
        sa.Column("run_id", sa.String(length=36), sa.ForeignKey("scrape_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("discovered_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("run_id", "job_id"),
    )


def _create_table_applications() -> None:
    op.create_table(
        "applications",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempted_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("confirmation_id", sa.String(length=256), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_applications_user_id ON applications (user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_applications_job_id ON applications (job_id)")


def _create_table_semantic_cache() -> None:
    op.create_table(
        "semantic_cache",
        sa.Column("key", sa.String(length=191), primary_key=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_semantic_cache_kind ON semantic_cache (kind)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_semantic_cache_user_id ON semantic_cache (user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_semantic_cache_source_hash ON semantic_cache (source_hash)")


def _create_table_board_account_credentials() -> None:
    op.create_table(
        "board_account_credentials",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("board", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=256), nullable=True),
        sa.Column("encrypted_secret_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "board"),
    )


def _create_table_board_sessions() -> None:
    op.create_table(
        "board_sessions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("board", sa.String(length=64), nullable=False),
        sa.Column("account_key", sa.String(length=128), nullable=False),
        sa.Column("account_username", sa.String(length=256), nullable=True),
        sa.Column("session_kind", sa.String(length=32), nullable=False),
        sa.Column("auth_state", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.Column("challenge_kind", sa.String(length=64), nullable=True),
        sa.Column("cookie_path", sa.String(length=512), nullable=True),
        sa.Column("session_path", sa.String(length=512), nullable=True),
        sa.Column("last_validated_at", sa.DateTime(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "board", "account_key", "session_kind", name="uq_board_session_identity_kind"),
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_board_sessions_user_id ON board_sessions (user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_board_sessions_board ON board_sessions (board)")


def _ensure_tables_exist(bind) -> None:
    tables = _table_names(bind)
    if "users" not in tables:
        _create_table_users()
    tables = _table_names(bind)
    if "user_profiles" not in tables:
        _create_table_user_profiles()
    if "user_settings" not in tables:
        _create_table_user_settings()
    if "scrape_runs" not in tables:
        _create_table_scrape_runs()
    if "run_events" not in tables:
        _create_table_run_events()
    if "run_executions" not in tables:
        _create_table_run_executions()
    if "jobs" not in tables:
        _create_table_jobs()
    if "run_jobs" not in tables:
        _create_table_run_jobs()
    if "applications" not in tables:
        _create_table_applications()
    if "semantic_cache" not in tables:
        _create_table_semantic_cache()
    if "board_account_credentials" not in tables:
        _create_table_board_account_credentials()
    if "board_sessions" not in tables:
        _create_table_board_sessions()


def _ensure_scrape_run_columns(bind) -> None:
    columns = _column_names(bind, "scrape_runs")
    if "trigger_type" not in columns:
        op.add_column("scrape_runs", sa.Column("trigger_type", sa.String(length=32), nullable=True))
    if "search_criteria_json" not in columns:
        op.add_column("scrape_runs", sa.Column("search_criteria_json", sa.Text(), nullable=True))
    if "saved_search_key" not in columns:
        op.add_column("scrape_runs", sa.Column("saved_search_key", sa.String(length=64), nullable=True))


def _ensure_jobs_normalized_url(bind) -> None:
    columns = _column_names(bind, "jobs")
    if "normalized_url" not in columns:
        op.add_column("jobs", sa.Column("normalized_url", sa.Text(), nullable=True))


def _backfill_normalized_urls(bind) -> None:
    rows = bind.execute(sa.text("SELECT id, url FROM jobs")).mappings().all()
    for row in rows:
        bind.execute(
            sa.text("UPDATE jobs SET normalized_url = :normalized_url WHERE id = :job_id"),
            {
                "normalized_url": _normalize_job_url(row["url"]),
                "job_id": row["id"],
            },
        )


def _dedupe_jobs(bind) -> None:
    tables = _table_names(bind)
    latest_run_by_job: dict[int, datetime] = {}
    if "run_jobs" in tables and "scrape_runs" in tables:
        linked_rows = bind.execute(
            sa.text(
                """
                SELECT run_jobs.job_id, MAX(scrape_runs.started_at) AS latest_run_at
                FROM run_jobs
                JOIN scrape_runs ON scrape_runs.id = run_jobs.run_id
                GROUP BY run_jobs.job_id
                """
            )
        ).mappings().all()
        latest_run_by_job = {
            row["job_id"]: _parse_dt(row["latest_run_at"])
            for row in linked_rows
        }

    job_rows = bind.execute(
        sa.text(
            """
            SELECT
                jobs.id,
                jobs.user_id,
                jobs.source_board,
                jobs.external_id,
                jobs.normalized_url,
                jobs.application_status,
                jobs.scraped_at,
                jobs.applied_at,
                jobs.scrape_run_id,
                scrape_runs.started_at AS scrape_run_started_at
            FROM jobs
            LEFT JOIN scrape_runs ON scrape_runs.id = jobs.scrape_run_id
            """
        )
    ).mappings().all()

    parent: dict[int, int] = {}

    def find(node: int) -> int:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    by_url: dict[tuple[int, str], int] = {}
    by_external: dict[tuple[int, str, str], int] = {}

    for row in job_rows:
        job_id = int(row["id"])
        user_id = row["user_id"]
        if user_id is None:
            continue
        normalized_url = row["normalized_url"]
        if normalized_url:
            key = (int(user_id), str(normalized_url))
            if key in by_url:
                union(job_id, by_url[key])
            else:
                by_url[key] = job_id
        external_id = row["external_id"]
        if external_id:
            key = (int(user_id), str(row["source_board"]), str(external_id))
            if key in by_external:
                union(job_id, by_external[key])
            else:
                by_external[key] = job_id

    grouped: dict[int, list[dict]] = {}
    for row in job_rows:
        root = find(int(row["id"]))
        grouped.setdefault(root, []).append(dict(row))

    for rows in grouped.values():
        if len(rows) <= 1:
            continue

        def sort_key(row: dict) -> tuple[int, datetime, datetime, datetime, int]:
            latest_run_at = max(
                _parse_dt(row.get("scrape_run_started_at")),
                latest_run_by_job.get(int(row["id"]), datetime.min),
            )
            return (
                _status_priority(row.get("application_status")),
                latest_run_at,
                _parse_dt(row.get("applied_at")),
                _parse_dt(row.get("scraped_at")),
                int(row["id"]),
            )

        rows.sort(key=sort_key, reverse=True)
        keep = rows[0]
        best_scrape_run = max(
            rows,
            key=lambda row: _parse_dt(row.get("scrape_run_started_at")),
        ).get("scrape_run_id")
        if best_scrape_run and keep.get("scrape_run_id") != best_scrape_run:
            bind.execute(
                sa.text("UPDATE jobs SET scrape_run_id = :run_id WHERE id = :job_id"),
                {"run_id": best_scrape_run, "job_id": keep["id"]},
            )

        for duplicate in rows[1:]:
            bind.execute(
                sa.text("UPDATE applications SET job_id = :keep_id WHERE job_id = :duplicate_id"),
                {"keep_id": keep["id"], "duplicate_id": duplicate["id"]},
            )
            if "run_jobs" in tables:
                bind.execute(
                    sa.text(
                        """
                        INSERT INTO run_jobs (run_id, job_id, discovered_at)
                        SELECT dup.run_id, :keep_id, dup.discovered_at
                        FROM run_jobs AS dup
                        WHERE dup.job_id = :duplicate_id
                          AND NOT EXISTS (
                              SELECT 1
                              FROM run_jobs AS existing
                              WHERE existing.run_id = dup.run_id
                                AND existing.job_id = :keep_id
                          )
                        """
                    ),
                    {"keep_id": keep["id"], "duplicate_id": duplicate["id"]},
                )
                bind.execute(
                    sa.text("DELETE FROM run_jobs WHERE job_id = :duplicate_id"),
                    {"duplicate_id": duplicate["id"]},
                )
            bind.execute(
                sa.text("DELETE FROM jobs WHERE id = :duplicate_id"),
                {"duplicate_id": duplicate["id"]},
            )


def _ensure_application_job_fk(bind) -> None:
    if not _has_fk(bind, "applications", "job_id", "jobs"):
        with op.batch_alter_table("applications", recreate="always") as batch_op:
            batch_op.create_foreign_key(
                "fk_applications_job_id_jobs",
                "jobs",
                ["job_id"],
                ["id"],
                ondelete="CASCADE",
            )


def _ensure_indexes() -> None:
    op.execute("CREATE INDEX IF NOT EXISTS ix_run_events_run_id_id ON run_events (run_id, id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_run_executions_state_created_at "
        "ON run_executions (state, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_jobs_user_status_scraped_at "
        "ON jobs (user_id, application_status, scraped_at)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_user_normalized_url "
        "ON jobs (user_id, normalized_url)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_user_source_external_id "
        "ON jobs (user_id, source_board, external_id) "
        "WHERE external_id IS NOT NULL"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_applications_job_id ON applications (job_id)")


def upgrade() -> None:
    bind = op.get_bind()
    _ensure_tables_exist(bind)
    _ensure_scrape_run_columns(bind)
    _ensure_jobs_normalized_url(bind)
    _backfill_normalized_urls(bind)
    _dedupe_jobs(bind)
    _ensure_application_job_fk(bind)
    _ensure_indexes()


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_jobs_user_source_external_id")
    op.execute("DROP INDEX IF EXISTS uq_jobs_user_normalized_url")
    op.execute("DROP INDEX IF EXISTS ix_jobs_user_status_scraped_at")
    op.execute("DROP INDEX IF EXISTS ix_run_executions_state_created_at")
    op.execute("DROP INDEX IF EXISTS ix_run_events_run_id_id")
    for table in [
        "board_sessions",
        "board_account_credentials",
        "semantic_cache",
        "applications",
        "run_jobs",
        "jobs",
        "run_executions",
        "run_events",
        "scrape_runs",
        "user_settings",
        "user_profiles",
        "users",
    ]:
        op.execute(sa.text(f"DROP TABLE IF EXISTS {table}"))
