"""Site login: profile passwords and sessions

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-15 23:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from job_finder.db.models import UTCDateTime

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("profiles", schema=None) as batch_op:
        batch_op.add_column(sa.Column("password_hash", sa.String(length=256), nullable=True))
    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(timezone=True), nullable=False),
        sa.Column("expires_at", UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_sessions_profile_id"), ["profile_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_sessions_profile_id"))
    op.drop_table("sessions")
    with op.batch_alter_table("profiles", schema=None) as batch_op:
        batch_op.drop_column("password_hash")
