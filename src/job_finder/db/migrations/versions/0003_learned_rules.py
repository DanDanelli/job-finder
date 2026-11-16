"""Learned rules from feedback

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-16 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from job_finder.db.models import UTCDateTime

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("profiles") as batch:
        batch.add_column(sa.Column("learned_rules", sa.JSON(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("rules_draft", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("rules_checked_at", UTCDateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("profiles") as batch:
        batch.drop_column("rules_checked_at")
        batch.drop_column("rules_draft")
        batch.drop_column("learned_rules")
