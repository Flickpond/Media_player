"""Let the uploader keep 60 fps.

Every ladder rung is capped at 30 fps since sprint 5 began, because a phone's
60 fps 4K costs twice as much to encode for motion most viewers never notice.
The uploader can opt out per video, and that choice has to outlive the request
that made it: it travels from the upload session to the job, and an edit
inherits it from the video it was made from.

30 for every existing row, which is what each of them was encoded at the next
time they are retried or edited.

Revision ID: 20261011_06
Revises: 20261008_05
"""

import sqlalchemy as sa
from alembic import op

revision = "20261011_06"
down_revision = "20261008_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("jobs", "upload_sessions"):
        op.add_column(
            table,
            sa.Column("max_fps", sa.SmallInteger(), nullable=False, server_default=sa.text("30")),
        )
    op.create_check_constraint("ck_jobs_max_fps", "jobs", "max_fps IN (30, 60)")
    op.create_check_constraint("ck_upload_max_fps", "upload_sessions", "max_fps IN (30, 60)")


def downgrade() -> None:
    op.drop_constraint("ck_upload_max_fps", "upload_sessions", type_="check")
    op.drop_constraint("ck_jobs_max_fps", "jobs", type_="check")
    for table in ("upload_sessions", "jobs"):
        op.drop_column(table, "max_fps")
