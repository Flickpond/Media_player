"""Record each video's seek-bar preview.

Sprint 6 plan, 2.2: hovering the seek bar shows that moment's frame, from a
sheet of small frames and a WebVTT index the worker makes after the ladder.
The API offers `preview_vtt_url` only once both exist, so it has to know --
without asking the bucket for every row of every page.

`preview_key` is the index's object key. Null on every existing row and on
any video whose preview was never made: the player then has no preview, as
before. A preview is made from a finished ladder's video, so the database
holds the two together.

Revision ID: 20261021_07
Revises: 20261011_06
"""

import sqlalchemy as sa
from alembic import op

revision = "20261021_07"
down_revision = "20261011_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("preview_key", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_jobs_preview_needs_ladder", "jobs", "preview_key IS NULL OR hls_key IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_constraint("ck_jobs_preview_needs_ladder", "jobs", type_="check")
    op.drop_column("jobs", "preview_key")
