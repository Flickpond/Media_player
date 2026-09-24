"""The one place a MinIO client is built.

There used to be three -- the upload path, the presigner, and the worker -- each
constructing a client from the same credentials and region. Identical
boilerplate in three files drifts: a change lands in one and is forgotten in the
others, which is how the presigner spent a day handing browsers `http://` URLs
after the site moved to TLS.

**There are deliberately two clients, not one.** They are not
interchangeable, and merging them breaks video playback in a way no test on
this project would catch:

* `internal_client()` talks to MinIO over the compose network, at
  `minio:9000`, in plain HTTP. Nothing external reaches it.
* `public_client()` exists only to sign URLs that a *browser* will open. The
  host it is built with ends up inside the SigV4 signature, so it must be the
  address the browser uses -- and its scheme must match the page's, or the
  browser blocks the URL as mixed content.

That is why the endpoint and the TLS flag are separate settings rather than one
shared pair. If you are ever tempted to collapse these into a single client,
read T-19 and the S2-05 history first.
"""

from functools import lru_cache

from minio import Minio

from app.config import get_settings


def _build(*, endpoint: str, secure: bool) -> Minio:
    settings = get_settings()
    return Minio(
        endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=secure,
        region=settings.minio_region,
    )


@lru_cache
def internal_client() -> Minio:
    """For reading and writing objects from inside the network."""
    settings = get_settings()
    return _build(endpoint=settings.minio_endpoint, secure=settings.minio_use_ssl)


@lru_cache
def public_client() -> Minio:
    """For signing URLs a browser will open. See the module docstring."""
    settings = get_settings()
    return _build(
        endpoint=settings.minio_public_endpoint,
        secure=settings.minio_public_use_ssl,
    )


def bucket() -> str:
    """The one bucket this project uses, read in one place."""
    return get_settings().minio_bucket
