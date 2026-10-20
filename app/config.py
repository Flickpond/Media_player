from functools import lru_cache
from urllib.parse import quote

from pydantic import Field, field_validator
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
    # On AWS the instance role supplies short-lived credentials, so no access
    # key exists to put in `.env` or to leak from it. Explicit rather than
    # inferred from an empty key: the key defaults to MinIO's, and silently
    # switching credential source on a blank value is how a misconfigured
    # host ends up talking to the wrong store.
    storage_use_instance_role: bool = False
    # Separate from minio_use_ssl on purpose. The internal client talks to
    # minio:9000 over plain HTTP inside the compose network and always will;
    # the presigned URLs handed to a browser must be https once the site is,
    # or the page blocks them as mixed content. One flag cannot be both.
    minio_public_use_ssl: bool = False
    output_url_expiry_seconds: int = 3600
    # CloudFront in front of the bucket, for HLS segments: the traffic that
    # grows with every viewer. All three empty -- the default, and every
    # local and Compose stack -- keeps signing segments against storage
    # directly. On AWS they come from SSM via deploy/aws/fetch-secrets.sh.
    # The key is a base64-encoded PEM, so it fits on one .env line.
    cloudfront_domain: str = ""
    cloudfront_key_pair_id: str = ""
    cloudfront_private_key_b64: str = ""
    # Bound each multipart upload before creating a session or storage object.
    max_upload_bytes: int = Field(default=2 * 1024**3, gt=0)

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
    # Ships in the same package as ffmpeg, so it is a new call rather than a
    # new dependency. Separate setting anyway: a deployment that points
    # `worker_ffmpeg_binary` at a custom build needs to say where the
    # matching probe is, and silently probing with a different version's
    # binary is the kind of mismatch that shows up as bad geometry, not as
    # an error.
    worker_ffprobe_binary: str = "ffprobe"
    worker_ffprobe_timeout_seconds: int = 30

    # Where the browser reaches this API, which is not where the API thinks
    # it lives: nginx proxies `/api/` and strips the prefix, so a path built
    # from the app's own routing table is missing it. Same problem, and the
    # same shape of answer, as `minio_public_endpoint`.
    #
    # Only HLS needs this. Every other URL the API hands out is either
    # absolute (a presigned object URL) or resolved by the page itself.
    api_public_prefix: str = "/api"
    worker_ffmpeg_preset: str = "veryfast"
    worker_ffmpeg_crf: int = 23
    # The single-file MP4 is the fallback every player can open, so it stays
    # at a size that plays and downloads anywhere. Quality above it comes
    # from the ladder.
    worker_ffmpeg_max_height: int = 1080
    # The tallest ladder rung. 1080 by default because that is what the
    # current two-core server can encode inside the job timeout; a host that
    # can encode 4K in time sets 2160. Never above the source either way.
    worker_hls_max_height: int = 1080
    worker_ffmpeg_timeout_seconds: int = 870
    # The ladder queue's own limit: the HD ladder, and the HDR and AV1
    # renditions after it. Longer than the MP4's, because the video already
    # plays by then and nobody waits on a slow ladder -- while one cut off at
    # the MP4's 30 minutes is never there at all: on flickpond.com a 4:55 4K
    # 60 fps upload lost its whole ladder at exactly 30 minutes (20 Oct
    # 2026). Set in code, not per service, because the API queues AV1 jobs
    # too and must give them the same limit. FFmpeg stops 30 s sooner, so a
    # timed-out encode is reported, not killed mid-write.
    worker_ladder_timeout_seconds: int = 3600
    # The HD ladder in pieces across workers (app/worker/ladder_pieces.py).
    # Off until proven on flickpond.com; helpers come from worker_chunk_helpers.
    worker_chunked_ladder: bool = False
    worker_ladder_piece_seconds: int = 30
    # Chunked MP4 (app/worker/chunked.py, sprint 6 plan 2.3). Off until a
    # 10-minute video has been timed and its joins checked on spot workers.
    worker_chunked_mp4: bool = False
    worker_chunk_seconds: int = 10
    # Shorter than this, one run is as fast: pieces cost a download each.
    worker_chunk_min_duration_seconds: int = 60
    # Helper jobs queued per video; the pool's size decides how many run.
    worker_chunk_helpers: int = 3
    # A helper's piece not done after this long is taken to be lost.
    worker_chunk_stale_seconds: int = 300
    # Extra AV1 renditions (app/worker/renditions.py), both off by default.
    # Keep HDR: an HDR upload also gets a 10-bit HDR rendition for HDR screens.
    worker_keep_hdr: bool = False
    # A video played this many times gets an AV1 rendition; 0 is off. Counted
    # by the API as players open the master playlist.
    av1_popular_views: int = 0
    # The longest source the worker accepts, checked right after the probe
    # and before any encoding. By duration rather than bytes: encode time --
    # and so whether a job finishes inside the timeout above -- follows how
    # long a video is, which a byte count no longer predicts once uploads are
    # 4K. Five minutes until a real 4K clip has been timed on the sprint 4
    # instance; 0 turns the check off.
    media_max_duration_seconds: int = 300
    # The poster frame's bounding box. Big enough for a Library card on a
    # high-density screen, small enough to stay a few tens of kilobytes.
    thumbnail_max_width: int = 640
    thumbnail_max_height: int = 360
    reaper_interval_seconds: int = 60
    reaper_lease_seconds: int = 1800
    # How long a job may be processing before the reaper asks RQ whether its
    # worker is still alive (the heartbeat in the started registry). A dead
    # worker is then found within minutes; `reaper_lease_seconds` remains the
    # backstop for one that is alive but stuck. Longer than RQ's 90-second
    # heartbeat expiry, so a slow heartbeat is never mistaken for a death.
    reaper_abandoned_seconds: int = 300
    reaper_orphan_grace_seconds: int = 3600

    # Queue depth published to CloudWatch (app/metrics/queue_depth.py), the
    # signal sprint 5's worker Auto Scaling Group scales on. The environment
    # is a dimension, so staging and production can share an account without
    # one scaling on the other's queue.
    metrics_namespace: str = "Flickpond"
    metrics_environment: str = "local"
    # No default: the region has to be where the scaling alarms live, and a
    # guessed one publishes a metric that nothing ever reads.
    metrics_region: str = ""
    metrics_interval_seconds: int = 60

    # The spot worker group the API wakes when a job is queued and it has no
    # worker (app/services/worker_wake.py). Empty: no group, nothing to wake.
    worker_group_name: str = ""
    worker_group_region: str = ""

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
