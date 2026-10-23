"""A library trash: deleting a video moves it there for a month first.

`deleted_at` is null for every existing row: nothing was in a trash before.

Revision ID: 20261023_07
Revises: 20261011_06
"""

import sqlalchemy as sa
from alembic import op

revision = "20261023_07"
down_revision = "20261011_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_jobs_deleted_at", "jobs", ["deleted_at"])


def downgrade() -> None:
    op.drop_index("ix_jobs_deleted_at", table_name="jobs")
    op.drop_column("jobs", "deleted_at")
