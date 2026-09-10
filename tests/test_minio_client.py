"""There are two MinIO clients, and they must not become one.

`internal_client()` reaches MinIO over the compose network. `public_client()`
exists only to sign URLs a browser opens: the host it is built with lands
inside the SigV4 signature, and its scheme has to match the page's or the
browser blocks the URL as mixed content.

Collapsing them looks like a tidy-up and breaks video playback in production
without failing anything else. These tests are the guard.
"""

import pytest
from minio import Minio

from app.config import Settings
from app.services import minio_client
from app.services.minio_client import bucket, internal_client, public_client

INTERNAL = "minio:9000"
PUBLIC = "flickpond.example"


@pytest.fixture(autouse=True)
def pinned(monkeypatch):
    """Pin settings rather than read the environment (T-13)."""
    settings = Settings(
        minio_endpoint=INTERNAL,
        minio_use_ssl=False,
        minio_public_endpoint=PUBLIC,
        minio_public_use_ssl=True,
        minio_bucket="videos",
        minio_region="us-east-1",
        minio_access_key="key",
        minio_secret_key="secret",
    )
    monkeypatch.setattr(minio_client, "get_settings", lambda: settings)
    internal_client.cache_clear()
    public_client.cache_clear()
    yield settings
    internal_client.cache_clear()
    public_client.cache_clear()


def test_both_are_minio_clients():
    assert isinstance(internal_client(), Minio)
    assert isinstance(public_client(), Minio)


def test_they_are_not_the_same_client():
    """The whole point of the module. If this ever passes as equal, playback
    is broken for anyone whose browser is not on the compose network.
    """
    assert internal_client() is not public_client()


def test_the_internal_client_uses_the_internal_endpoint_over_plain_http():
    url = internal_client()._base_url
    assert INTERNAL in str(url._url.netloc)
    assert url._url.scheme == "http", "the compose network does not speak TLS"


def test_the_public_client_uses_the_public_endpoint_over_https():
    """The signature covers this host, and the scheme has to match the page."""
    url = public_client()._base_url
    assert PUBLIC in str(url._url.netloc)
    assert url._url.scheme == "https"


def test_each_client_is_built_once():
    assert internal_client() is internal_client()
    assert public_client() is public_client()


def test_the_bucket_is_read_from_settings():
    assert bucket() == "videos"
