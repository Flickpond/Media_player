"""Settings that other components depend on being right.

`redis_url` is what the worker and the API both dial; `use_asyncpg_driver` is
what stops a plain `postgresql://` DSN from the compose file blowing up the
async engine. Both are pure functions of config, and both are silent when
wrong -- you find out by watching a worker fail to connect.
"""

from app.config import Settings


def make(**overrides) -> Settings:
    base = dict(redis_host="redis", redis_port=6379, redis_password="", redis_ssl=False)
    return Settings(**(base | overrides))


def test_redis_url_plain():
    assert make().redis_url == "redis://redis:6379/0"


def test_redis_url_switches_scheme_for_tls():
    """A cloud Redis with REDIS_SSL=true must produce rediss://, not redis://."""
    assert make(redis_ssl=True).redis_url == "rediss://redis:6379/0"


def test_redis_url_includes_a_password_when_set():
    assert make(redis_password="s3cret").redis_url == "redis://:s3cret@redis:6379/0"


def test_redis_url_with_tls_and_password():
    url = make(redis_ssl=True, redis_password="s3cret", redis_host="cache.example.com").redis_url

    assert url == "rediss://:s3cret@cache.example.com:6379/0"


def test_plain_postgres_dsn_is_rewritten_for_asyncpg():
    """Compose hands us postgresql://; SQLAlchemy's async engine needs +asyncpg."""
    settings = make(postgres_dsn="postgresql://u:p@postgres:5432/flickpond")

    assert settings.postgres_dsn == "postgresql+asyncpg://u:p@postgres:5432/flickpond"


def test_postgres_scheme_alias_is_also_rewritten():
    settings = make(postgres_dsn="postgres://u:p@postgres:5432/flickpond")

    assert settings.postgres_dsn == "postgresql+asyncpg://u:p@postgres:5432/flickpond"


def test_dsn_that_already_names_a_driver_is_left_alone():
    dsn = "postgresql+asyncpg://u:p@postgres:5432/flickpond"

    assert make(postgres_dsn=dsn).postgres_dsn == dsn
