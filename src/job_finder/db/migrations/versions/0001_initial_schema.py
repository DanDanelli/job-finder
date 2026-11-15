"""Initial schema

Revision ID: 0001
Revises:
Create Date: 2026-11-15 18:40:48.246582
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from job_finder.db.models import UTCDateTime

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("dedup_key", sa.String(length=512), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("company", sa.String(length=256), nullable=True),
        sa.Column("country", sa.String(length=2), nullable=True),
        sa.Column("city", sa.String(length=128), nullable=True),
        sa.Column("location_text", sa.String(length=512), nullable=True),
        sa.Column("eligible_locations", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("work_mode", sa.String(length=16), nullable=False),
        sa.Column("contract_type", sa.String(length=16), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("salary_min", sa.Float(), nullable=True),
        sa.Column("salary_max", sa.Float(), nullable=True),
        sa.Column("salary_currency", sa.String(length=3), nullable=True),
        sa.Column("salary_period", sa.String(length=8), nullable=True),
        sa.Column("posted_at", UTCDateTime(timezone=True), nullable=True),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("apply_url", sa.String(length=2048), nullable=True),
        sa.Column("created_at", UTCDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", UTCDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedup_key"),
    )
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_jobs_company"), ["company"], unique=False)
        batch_op.create_index(batch_op.f("ix_jobs_country"), ["country"], unique=False)
        batch_op.create_index(batch_op.f("ix_jobs_posted_at"), ["posted_at"], unique=False)

    op.create_table(
        "profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("queries", sa.JSON(), nullable=False),
        sa.Column("filters", sa.JSON(), nullable=False),
        sa.Column("preferences", sa.Text(), nullable=False),
        sa.Column("resume_path", sa.String(length=512), nullable=True),
        sa.Column("paused", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", UTCDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", UTCDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
        sa.UniqueConstraint("telegram_chat_id"),
    )
    op.create_table(
        "quota_usage",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("connector", sa.String(length=64), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connector", "day"),
    )
    op.create_table(
        "job_sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=256), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("raw_data", sa.JSON(), nullable=False),
        sa.Column("collected_at", UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "source_id"),
    )
    with op.batch_alter_table("job_sources", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_job_sources_job_id"), ["job_id"], unique=False)

    op.create_table(
        "matches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("prerank_score", sa.Float(), nullable=True),
        sa.Column("strengths", sa.JSON(), nullable=False),
        sa.Column("gaps", sa.JSON(), nullable=False),
        sa.Column("dealbreakers", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("notified_at", UTCDateTime(timezone=True), nullable=True),
        sa.Column("created_at", UTCDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "profile_id"),
    )
    with op.batch_alter_table("matches", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_matches_profile_id"), ["profile_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_matches_score"), ["score"], unique=False)
        batch_op.create_index(batch_op.f("ix_matches_status"), ["status"], unique=False)

    op.create_table(
        "runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", UTCDateTime(timezone=True), nullable=False),
        sa.Column("finished_at", UTCDateTime(timezone=True), nullable=True),
        sa.Column("jobs_collected", sa.Integer(), nullable=False),
        sa.Column("jobs_new", sa.Integer(), nullable=False),
        sa.Column("matches_new", sa.Integer(), nullable=False),
        sa.Column("errors", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "feedback",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("match_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["match_id"], ["matches.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("feedback", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_feedback_match_id"), ["match_id"], unique=False)

    op.create_table(
        "llm_costs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("profile_id", sa.Integer(), nullable=True),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("batch", sa.Boolean(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("created_at", UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("llm_costs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_llm_costs_created_at"), ["created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_llm_costs_run_id"), ["run_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("llm_costs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_llm_costs_run_id"))
        batch_op.drop_index(batch_op.f("ix_llm_costs_created_at"))

    op.drop_table("llm_costs")
    with op.batch_alter_table("feedback", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_feedback_match_id"))

    op.drop_table("feedback")
    op.drop_table("runs")
    with op.batch_alter_table("matches", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_matches_status"))
        batch_op.drop_index(batch_op.f("ix_matches_score"))
        batch_op.drop_index(batch_op.f("ix_matches_profile_id"))

    op.drop_table("matches")
    with op.batch_alter_table("job_sources", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_job_sources_job_id"))

    op.drop_table("job_sources")
    op.drop_table("quota_usage")
    op.drop_table("profiles")
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_jobs_posted_at"))
        batch_op.drop_index(batch_op.f("ix_jobs_country"))
        batch_op.drop_index(batch_op.f("ix_jobs_company"))

    op.drop_table("jobs")
