"""Add a job's editing operations, and the key of its HLS master playlist.

Revision ID: 20260918_03
Revises: 20260910_02
Create Date: 2026-09-18

Two columns, both nullable, neither backfilled -- the existing rows are
correct as they stand. A plain upload has no operations, and no job created
before this migration has an HLS ladder.

`operations` gets a CHECK and `hls_key` deliberately does not. The
constraint on `operations` is shape-only: a non-NULL value has to be a
non-empty JSON array, because "this job is an edit" and "this job was asked
to do nothing" are different things and only one of them is storable. What
is *in* the array is the worker's business, not the database's -- there is
no CHECK that could usefully validate an operation's parameters without
encoding the whole registry in SQL.

`hls_key` gets nothing, and specifically not the `status = 'done'` shape
that `ck_jobs_output_key` enforces for `output_key`. Building the ladder is
best-effort: if FFmpeg cannot produce one, the MP4 output still stands and
the job is legitimately `done` with `hls_key IS NULL`. Mirroring
`ck_jobs_output_key` here would turn that intended fallback into a write the
database rejects, and the worker would fail a job that had in fact succeeded.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260918_03"
down_revision: str | None = "20260910_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("operations", postgresql.JSONB(), nullable=True))
    op.add_column("jobs", sa.Column("hls_key", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_jobs_operations_nonempty_array",
        "jobs",
        "operations IS NULL OR "
        "(jsonb_typeof(operations) = 'array' AND jsonb_array_length(operations) > 0)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_jobs_operations_nonempty_array", "jobs", type_="check")
    op.drop_column("jobs", "hls_key")
    op.drop_column("jobs", "operations")
