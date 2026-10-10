"""HLS segments through CloudFront, signed so they stay as private as before.

A CloudFront signed URL is only as good as its signature: these check it
against the public key the way CloudFront does, not just that a parameter
called Signature is present.
"""

import base64
import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.services import output_urls
from app.services.output_urls import CloudFrontOutputUrlSigner, MinioOutputUrlSigner

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def storage():
    class Storage:
        async def create_url(self, key):
            return f"https://storage.example/{key}?download"

        async def create_inline_url(self, key):
            raise AssertionError("segments must not be signed against storage")

    return Storage()


def _pem(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _signer(storage, key):
    return CloudFrontOutputUrlSigner(
        storage,
        domain="d111.cloudfront.net",
        key_pair_id="K2JCJMDEHXQW5F",
        private_key_pem=_pem(key),
        expiry_seconds=3600,
        now=lambda: NOW,
    )


def _verify(url: str, public_key) -> None:
    """What CloudFront does: rebuild the canned policy and check the signature."""
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    resource = f"{parts.scheme}://{parts.netloc}{parts.path}"
    expires = int(query["Expires"][0])
    condition = {"DateLessThan": {"AWS:EpochTime": expires}}
    policy = json.dumps(
        {"Statement": [{"Resource": resource, "Condition": condition}]},
        separators=(",", ":"),
    ).encode()
    signature = base64.b64decode(
        query["Signature"][0].replace("-", "+").replace("_", "=").replace("~", "/")
    )
    public_key.verify(signature, policy, padding.PKCS1v15(), hashes.SHA1())


async def test_a_segment_url_is_on_the_cdn_and_verifies_against_the_public_key(storage, key):
    url = await _signer(storage, key).create_inline_url("outputs/abc/hls/v2/seg00001.ts")

    parts = urlsplit(url)
    assert (parts.scheme, parts.netloc, parts.path) == (
        "https",
        "d111.cloudfront.net",
        "/outputs/abc/hls/v2/seg00001.ts",
    )
    assert parse_qs(parts.query)["Key-Pair-Id"] == ["K2JCJMDEHXQW5F"]
    _verify(url, key.public_key())


async def test_the_url_expires_like_the_storage_urls_did(storage, key):
    url = await _signer(storage, key).create_inline_url("outputs/abc/hls/master.m3u8")

    expires = int(parse_qs(urlsplit(url).query)["Expires"][0])
    assert expires == int(NOW.timestamp()) + 3600


async def test_a_url_altered_to_another_object_fails_verification(storage, key):
    """The signature covers the path: one user's URL cannot be edited into another's."""
    url = await _signer(storage, key).create_inline_url("outputs/abc/hls/v2/seg00001.ts")

    with pytest.raises(InvalidSignature):
        _verify(url.replace("/abc/", "/xyz/"), key.public_key())


async def test_the_mp4_and_thumbnail_keep_the_storage_url_with_its_forced_download(storage, key):
    url = await _signer(storage, key).create_url("outputs/abc/clip.mp4")

    assert url == "https://storage.example/outputs/abc/clip.mp4?download"


async def test_a_key_with_unusual_characters_is_escaped_in_the_path(storage, key):
    url = await _signer(storage, key).create_inline_url("outputs/abc/hls/v0/seg 1.ts")

    assert "/seg%201.ts" in url
    _verify(url, key.public_key())


# --- choosing the signer ------------------------------------------------------


@pytest.fixture
def settings(monkeypatch):
    from types import SimpleNamespace

    current = SimpleNamespace(
        output_url_expiry_seconds=3600,
        cloudfront_domain="",
        cloudfront_key_pair_id="",
        cloudfront_private_key_b64="",
    )
    monkeypatch.setattr(output_urls, "get_settings", lambda: current)
    monkeypatch.setattr(output_urls, "public_client", lambda: object())
    monkeypatch.setattr(output_urls, "bucket", lambda: "videos")
    output_urls.get_output_url_signer.cache_clear()
    yield current
    output_urls.get_output_url_signer.cache_clear()


def test_without_cloudfront_every_url_is_signed_against_storage(settings):
    assert isinstance(output_urls.get_output_url_signer(), MinioOutputUrlSigner)


def test_with_cloudfront_configured_segments_go_through_it(settings, key):
    settings.cloudfront_domain = "d111.cloudfront.net"
    settings.cloudfront_key_pair_id = "K2JCJMDEHXQW5F"
    settings.cloudfront_private_key_b64 = base64.b64encode(_pem(key)).decode()

    assert isinstance(output_urls.get_output_url_signer(), CloudFrontOutputUrlSigner)


def test_a_domain_without_its_key_fails_at_startup_not_at_playback(settings):
    settings.cloudfront_domain = "d111.cloudfront.net"

    with pytest.raises(RuntimeError, match="key pair id and private key"):
        output_urls.get_output_url_signer()
