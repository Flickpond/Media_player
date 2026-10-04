"""Add a job's media dimensions, its thumbnail, and the state of its HLS ladder.

Revision ID: 20261004_04
Revises: 20260918_03
Create Date: 2026-10-04

Sprint 4's only migration; every track reads these columns.

`width`, `height` and `duration_seconds` are what the source actually is,
filled in once the worker has probed it. They let the editor offer only
operations that make sense for this video, and let `/edit` reject an
impossible scale request immediately instead of failing the job later.

`thumbnail_key` is the poster frame, once one has been extracted.

`hls_status` says where the adaptive ladder stands -- `pending`, `ready` or
`unavailable` -- so the page can say "HD versions are still processing"
instead of guessing from a missing `hls_key`. It is constrained to agree with
`hls_key`: a ladder is `ready` exactly when its key exists. Same posture as
`ck_jobs_output_key` and `ck_jobs_error`: the database is the last line of
defence against the two drifting apart.

Existing rows are backfilled from what they already say: a job with a ladder
is `ready`; one still in flight is `pending`; anything else -- finished
without a ladder, failed, or from before sprint 3 -- is `unavailable`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261004_04"
down_revision: str | None = "20260918_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("width", sa.Integer(), nullable=True))
    op.add_column("jobs", sa.Column("height", sa.Integer(), nullable=True))
    op.add_column("jobs", sa.Column("duration_seconds", sa.Float(), nullable=True))
    op.add_column("jobs", sa.Column("thumbnail_key", sa.Text(), nullable=True))

    # Nullable for the backfill, then NOT NULL with a default -- one migration,
    # so no deployed database ever has the column half-populated.
    op.add_column("jobs", sa.Column("hls_status", sa.Text(), nullable=True))
    op.execute(
        "UPDATE jobs SET hls_status = CASE "
        "WHEN hls_key IS NOT NULL THEN 'ready' "
        "WHEN status IN ('queued', 'processing') THEN 'pending' "
        "ELSE 'unavailable' END"
    )
    op.alter_column(
        "jobs", "hls_status", nullable=False, server_default=sa.text("'pending'")
    )

    op.create_check_constraint(
        "ck_jobs_hls_status",
        "jobs",
        "hls_status IN ('pending', 'ready', 'unavailable')",
    )
    op.create_check_constraint(
        "ck_jobs_hls_ready_has_key",
        "jobs",
        "(hls_status = 'ready') = (hls_key IS NOT NULL)",
    )
    # A probe never reports zero or negative values for a readable video, so
    # one stored here means something wrote the column without probing.
    op.create_check_constraint(
        "ck_jobs_media_dimensions_positive",
        "jobs",
        "(width IS NULL OR width > 0) AND (height IS NULL OR height > 0) "
        "AND (duration_seconds IS NULL OR duration_seconds > 0)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_jobs_media_dimensions_positive", "jobs", type_="check")
    op.drop_constraint("ck_jobs_hls_ready_has_key", "jobs", type_="check")
    op.drop_constraint("ck_jobs_hls_status", "jobs", type_="check")
    op.drop_column("jobs", "hls_status")
    op.drop_column("jobs", "thumbnail_key")
    op.drop_column("jobs", "duration_seconds")
    op.drop_column("jobs", "height")
    op.drop_column("jobs", "width")
