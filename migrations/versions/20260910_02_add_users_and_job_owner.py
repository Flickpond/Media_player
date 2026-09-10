"""Add users, and give every job an owner.

Revision ID: 20260910_02
Revises: 20260904_01
Create Date: 2026-09-10

The backfill and the NOT NULL both happen here, in one migration, on purpose.
Existing rows predate accounts, so `owner_id` has to start nullable -- but
leaving it that way permanently means every scoped query carries a null branch
forever, and one of them will eventually forget. Splitting this across two
migrations would also leave a window where a deployed database has the column
nullable, which is exactly when someone writes the query that assumes it is not.

Rows are backfilled to a sentinel user whose password hash cannot match any
input, so the account exists to own history and cannot be logged into.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260910_02"
down_revision: str | None = "20260904_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_USER_EMAIL = "legacy@flickpond.invalid"
# Not a bcrypt hash and not any other recognised format, so verification always
# fails rather than accidentally matching. Deliberately not a valid hash.
UNUSABLE_PASSWORD_HASH = "!nologin"


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), server_default=sa.text("'user'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("role IN ('user', 'operator')", name="ck_users_role"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.add_column("jobs", sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True))

    # One sentinel row, created only if this migration has not already made it.
    op.execute(
        sa.text(
            "INSERT INTO users (id, email, password_hash, role) "
            "VALUES (gen_random_uuid(), :email, :hash, 'user') "
            "ON CONFLICT (email) DO NOTHING"
        ).bindparams(email=LEGACY_USER_EMAIL, hash=UNUSABLE_PASSWORD_HASH)
    )
    op.execute(
        sa.text(
            "UPDATE jobs SET owner_id = (SELECT id FROM users WHERE email = :email) "
            "WHERE owner_id IS NULL"
        ).bindparams(email=LEGACY_USER_EMAIL)
    )

    op.alter_column("jobs", "owner_id", nullable=False)
    op.create_foreign_key("fk_jobs_owner_id", "jobs", "users", ["owner_id"], ["id"])
    # Every scoped query filters on this, so it is indexed from the start.
    op.create_index("ix_jobs_owner_id", "jobs", ["owner_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_jobs_owner_id", table_name="jobs")
    op.drop_constraint("fk_jobs_owner_id", "jobs", type_="foreignkey")
    op.drop_column("jobs", "owner_id")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
