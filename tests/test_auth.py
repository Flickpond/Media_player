"""Authentication: the token, the cookie, and who is refused.

The endpoint-level scoping is in test_authorization.py; this file covers the
pieces that decide whether a caller is who they say they are.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest

from app.config import Settings
from app.services import security
from app.services.security import (
    COOKIE_NAME,
    InvalidTokenError,
    hash_password,
    issue_token,
    read_token,
    verify_password,
)

# At least MIN_SECRET_BYTES, so these tests exercise the real path rather
# than the "secret too short" guard.
SECRET = "test-secret-not-a-real-one-but-long-enough-for-hs256"


@pytest.fixture(autouse=True)
def pinned_settings(monkeypatch):
    """Pin the secret rather than reading the environment (T-13).

    Settings loads `.env`, so without this the suite would pass or fail
    depending on whether the developer running it has a configured stack.
    """
    settings = Settings(jwt_secret=SECRET, jwt_algorithm="HS256", jwt_ttl_seconds=1800)
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    return settings


# --- passwords -------------------------------------------------------------


def test_a_password_verifies_against_its_own_hash():
    assert verify_password("correct horse battery", hash_password("correct horse battery"))


def test_a_wrong_password_does_not_verify():
    assert not verify_password("wrong", hash_password("correct horse battery"))


def test_the_same_password_hashes_differently_each_time():
    """A shared salt would make identical passwords visibly identical."""
    assert hash_password("same") != hash_password("same")


def test_a_password_past_bcrypt_s_limit_is_refused_rather_than_truncated():
    """bcrypt ignores everything past 72 bytes, so a 200-character passphrase
    would be silently no stronger than its first 72. Reject instead.
    """
    with pytest.raises(ValueError, match="72 bytes"):
        hash_password("x" * 73)


def test_an_unusable_hash_returns_false_rather_than_raising():
    """The sentinel `legacy` account carries a deliberately invalid hash. A
    raise here would turn a failed login into a 500 and reveal the account.
    """
    assert not verify_password("anything", "!nologin")


# --- tokens ----------------------------------------------------------------


def test_a_token_round_trips():
    user_id = uuid4()
    subject, role = read_token(issue_token(user_id=user_id, role="user"))

    assert subject == user_id
    assert role == "user"


def test_a_token_signed_with_another_secret_is_refused():
    forged = jwt.encode({"sub": str(uuid4()), "role": "operator"}, "not-our-secret")

    with pytest.raises(InvalidTokenError):
        read_token(forged)


def test_an_expired_token_is_refused():
    past = datetime.now(UTC) - timedelta(hours=1)
    stale = jwt.encode(
        {"sub": str(uuid4()), "role": "user", "iat": past, "exp": past + timedelta(minutes=1)},
        SECRET,
        algorithm="HS256",
    )

    with pytest.raises(InvalidTokenError):
        read_token(stale)


def test_a_tampered_role_is_refused():
    """The signature covers the claims, so promoting yourself breaks it.

    The payload is base64url, so this decodes it, rewrites the role, and
    re-encodes with the original signature -- which is what an attacker with a
    token and no secret can actually do.
    """
    import base64
    import json

    token = issue_token(user_id=uuid4(), role="user")
    header, payload, signature = token.split(".")

    def b64d(part: str) -> bytes:
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))

    claims = json.loads(b64d(payload))
    assert claims["role"] == "user"
    claims["role"] = "operator"
    forged_payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()

    with pytest.raises(InvalidTokenError):
        read_token(f"{header}.{forged_payload}.{signature}")


def test_garbage_is_refused_rather_than_crashing():
    for junk in ["", "not.a.token", "a.b.c", "....."]:
        with pytest.raises(InvalidTokenError):
            read_token(junk)


def test_issuing_without_a_secret_fails_loudly(monkeypatch):
    """An unset secret must stop the process, not sign with an empty string."""
    monkeypatch.setattr(security, "get_settings", lambda: Settings(jwt_secret=""))

    with pytest.raises(RuntimeError, match="JWT_SECRET is not set"):
        issue_token(user_id=uuid4(), role="user")


def test_a_short_secret_is_refused_rather_than_warned_about(monkeypatch):
    """PyJWT only warns below 32 bytes, and a warning in a log nobody reads is
    not a control. RFC 7518 3.2 wants the key at least as long as the hash.
    """
    monkeypatch.setattr(security, "get_settings", lambda: Settings(jwt_secret="too-short"))

    with pytest.raises(RuntimeError, match="at least 32 bytes"):
        issue_token(user_id=uuid4(), role="user")


# --- the cookie ------------------------------------------------------------


def test_the_cookie_is_httponly_and_samesite_strict():
    """The whole point of the design: script cannot read this cookie.

    Asserted on the header rather than by hand, because a future refactor that
    drops httponly would otherwise pass every other test in this file.
    """
    from fastapi import Response

    from app.api.auth import _set_cookie

    response = Response()
    _set_cookie(response, "a.b.c")
    header = response.headers["set-cookie"]

    assert COOKIE_NAME in header
    assert "HttpOnly" in header
    assert "SameSite=strict" in header.replace("samesite", "SameSite")
    assert "Secure" in header
