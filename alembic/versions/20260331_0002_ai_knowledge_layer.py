"""ai knowledge layer foundation

Revision ID: 20260331_0002
Revises: 20260331_0001
Create Date: 2026-03-31 00:30:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260331_0002"
down_revision = "20260331_0001"
branch_labels = None
depends_on = None


def _table_names(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _column_names(bind, table_name: str) -> set[str]:
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def _create_candidate_knowledge_packs() -> None:
    op.create_table(
        "candidate_knowledge_packs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("embedding_json", sa.Text(), nullable=True),
        sa.Column("embedding_model", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "user_id",
            "version",
            "source_hash",
            name="uq_candidate_knowledge_pack_user_version_source",
        ),
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_candidate_knowledge_packs_user_id ON candidate_knowledge_packs (user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_candidate_knowledge_packs_version ON candidate_knowledge_packs (version)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_candidate_knowledge_packs_source_hash ON candidate_knowledge_packs (source_hash)")


def _create_job_knowledge_packs() -> None:
    op.create_table(
        "job_knowledge_packs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("embedding_json", sa.Text(), nullable=True),
        sa.Column("embedding_model", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "job_id",
            "version",
            "source_hash",
            name="uq_job_knowledge_pack_job_version_source",
        ),
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_job_knowledge_packs_job_id ON job_knowledge_packs (job_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_job_knowledge_packs_user_id ON job_knowledge_packs (user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_job_knowledge_packs_version ON job_knowledge_packs (version)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_job_knowledge_packs_source_hash ON job_knowledge_packs (source_hash)")


def _create_answer_memory() -> None:
    op.create_table(
        "answer_memory",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("question_key", sa.String(length=191), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("answer_text", sa.Text(), nullable=False),
        sa.Column("answer_type", sa.String(length=32), nullable=False, server_default="text"),
        sa.Column("source_kind", sa.String(length=32), nullable=False, server_default="learned"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1"),
        sa.Column("approved", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("evidence_json", sa.Text(), nullable=True),
        sa.Column("embedding_json", sa.Text(), nullable=True),
        sa.Column("embedding_model", sa.String(length=64), nullable=True),
        sa.Column("usage_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "question_key", name="uq_answer_memory_user_question_key"),
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_answer_memory_user_id ON answer_memory (user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_answer_memory_question_key ON answer_memory (question_key)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_answer_memory_user_updated_at ON answer_memory (user_id, updated_at)")


def upgrade() -> None:
    bind = op.get_bind()
    tables = _table_names(bind)

    if "candidate_knowledge_packs" not in tables:
        _create_candidate_knowledge_packs()
    if "job_knowledge_packs" not in tables:
        _create_job_knowledge_packs()
    if "answer_memory" not in tables:
        _create_answer_memory()

    semantic_cache_columns = _column_names(bind, "semantic_cache")
    if "prompt_name" not in semantic_cache_columns:
        op.add_column("semantic_cache", sa.Column("prompt_name", sa.String(length=64), nullable=True))
    if "prompt_version" not in semantic_cache_columns:
        op.add_column("semantic_cache", sa.Column("prompt_version", sa.String(length=32), nullable=True))
    if "model_name" not in semantic_cache_columns:
        op.add_column("semantic_cache", sa.Column("model_name", sa.String(length=128), nullable=True))
    if "metadata_json" not in semantic_cache_columns:
        op.add_column("semantic_cache", sa.Column("metadata_json", sa.Text(), nullable=True))

    op.execute("CREATE INDEX IF NOT EXISTS ix_semantic_cache_prompt_name ON semantic_cache (prompt_name)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_semantic_cache_prompt_name")
    for column_name in ("metadata_json", "model_name", "prompt_version", "prompt_name"):
        try:
            op.drop_column("semantic_cache", column_name)
        except Exception:
            pass

    op.drop_table("answer_memory")
    op.drop_table("job_knowledge_packs")
    op.drop_table("candidate_knowledge_packs")
