"""Persist resumable direct uploads independently of processing jobs."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261008_05"
down_revision = "20261004_04"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "upload_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=False, unique=True),
        sa.Column("storage_id", sa.Text(), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("part_size", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("manifest", postgresql.JSONB(), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("size > 0 AND part_size > 0", name="ck_upload_size"),
        sa.CheckConstraint(
            "state IN ('open', 'completing', 'ready', 'completed', 'aborting', 'aborted')",
            name="ck_upload_state",
        ),
    )
    op.create_index("ix_upload_sessions_owner_id", "upload_sessions", ["owner_id"])
    op.create_index("ix_upload_sessions_expires_at", "upload_sessions", ["expires_at"])


def downgrade():
    op.drop_table("upload_sessions")
