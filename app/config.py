from functools import lru_cache

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
    output_url_expiry_seconds: int = 3600

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
