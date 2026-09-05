"""The DSN driver rewrite.

Compose hands the app a plain `postgresql://` URL; SQLAlchemy's async engine
needs `postgresql+asyncpg://`. When this is wrong nothing fails until the first
query, so it is worth pinning.
"""

from app.config import Settings


def test_plain_postgres_dsn_is_rewritten_for_asyncpg():
    settings = Settings(postgres_dsn="postgresql://u:p@postgres:5432/flickpond")

    assert settings.postgres_dsn == "postgresql+asyncpg://u:p@postgres:5432/flickpond"


def test_postgres_scheme_alias_is_also_rewritten():
    settings = Settings(postgres_dsn="postgres://u:p@postgres:5432/flickpond")

    assert settings.postgres_dsn == "postgresql+asyncpg://u:p@postgres:5432/flickpond"


def test_dsn_that_already_names_a_driver_is_left_alone():
    dsn = "postgresql+asyncpg://u:p@postgres:5432/flickpond"

    assert Settings(postgres_dsn=dsn).postgres_dsn == dsn
