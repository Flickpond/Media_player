from functools import lru_cache
from urllib.parse import quote

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    postgres_dsn: str = (
        "postgresql+asyncpg://flickpond:flickpond_dev_password@127.0.0.1:5432/flickpond"
    )

    minio_endpoint: str = "127.0.0.1:9000"
    minio_public_endpoint: str = "127.0.0.1:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin123"
    minio_bucket: str = "videos"
    minio_region: str = "us-east-1"
    minio_use_ssl: bool = False
    # Separate from minio_use_ssl on purpose. The internal client talks to
    # minio:9000 over plain HTTP inside the compose network and always will;
    # the presigned URLs handed to a browser must be https once the site is,
    # or the page blocks them as mixed content. One flag cannot be both.
    minio_public_use_ssl: bool = False
    output_url_expiry_seconds: int = 3600

    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_password: str = ""
    redis_ssl: bool = False
    redis_queue: str = "video_jobs"

    # No default: a signing secret that falls back to something predictable is
    # worse than one that fails loudly on startup. Must be set in the
    # environment, and compose has to pass it through (T-19).
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    # Short, because a JWT cannot be revoked -- logout can only clear the
    # client's copy. This is the window a stolen token stays usable.
    jwt_ttl_seconds: int = 1800

    worker_output_prefix: str = "outputs"
    worker_job_timeout_seconds: int = 900
    worker_ffmpeg_binary: str = "ffmpeg"
    worker_ffmpeg_preset: str = "veryfast"
    worker_ffmpeg_crf: int = 23
    worker_ffmpeg_max_height: int = 720
    worker_ffmpeg_timeout_seconds: int = 870
    reaper_interval_seconds: int = 60
    reaper_lease_seconds: int = 1800
    reaper_orphan_grace_seconds: int = 3600

    @property
    def redis_url(self) -> str:
        scheme = "rediss" if self.redis_ssl else "redis"
        # Percent-encode the password: it lands in the userinfo part of a URL,
        # so an unescaped `@` or `/` in a real secret would silently repoint
        # the client at a different host instead of failing loudly.
        password = quote(self.redis_password, safe="")
        credentials = f":{password}@" if self.redis_password else ""
        return f"{scheme}://{credentials}{self.redis_host}:{self.redis_port}/0"

    @field_validator("postgres_dsn")
    @classmethod
    def use_asyncpg_driver(cls, value: str) -> str:
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+asyncpg://", 1)
        if value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql+asyncpg://", 1)
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
