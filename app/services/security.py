"""Password hashing and token issuing.

Kept apart from the endpoints so there is one place to audit the two things
that decide whether authentication means anything.

The token is a signed JWT, per the proposal, but it is delivered in an
HttpOnly cookie rather than an Authorization header -- see
docs/s2-03-auth-design.md. The consequence that matters here: a JWT cannot be
revoked. Logout clears the client's copy and nothing else, so the lifetime is
deliberately short.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import bcrypt
import jwt

from app.config import get_settings

# The cookie the browser sends back on every request.
COOKIE_NAME = "access_token"

# RFC 7518 §3.2: an HMAC key for HS256 should be at least as long as the hash
# output. PyJWT only warns about a shorter one, which is easy to never see --
# refuse it instead, at the point the secret is first used.
MIN_SECRET_BYTES = 32


class InvalidTokenError(Exception):
    """The token is missing, malformed, expired, or signed by someone else."""


def _checked_settings():
    """Refuse to sign or verify with a secret that is missing or too short.

    Checked here rather than in Settings so importing the app does not require
    a secret -- migrations, the worker and the reaper all import app.config and
    none of them issue tokens.
    """
    settings = get_settings()
    if not settings.jwt_secret:
        raise RuntimeError("JWT_SECRET is not set")
    if len(settings.jwt_secret.encode("utf-8")) < MIN_SECRET_BYTES:
        raise RuntimeError(
            f"JWT_SECRET must be at least {MIN_SECRET_BYTES} bytes; "
            "generate one with `openssl rand -base64 48`"
        )
    return settings


def hash_password(password: str) -> str:
    # bcrypt silently truncates at 72 bytes, so a longer passphrase would have
    # its tail ignored -- reject rather than quietly weaken it.
    encoded = password.encode("utf-8")
    if len(encoded) > 72:
        raise ValueError("password must be at most 72 bytes")
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time where it matters, and never raises on a malformed hash.

    The sentinel `legacy` user carries a deliberately unusable hash, and bcrypt
    raises on anything that is not a valid hash string. A raise here would turn
    a failed login into a 500 and leak which accounts are real.
    """
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def issue_token(*, user_id: UUID, role: str) -> str:
    settings = _checked_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "role": role,
        "iat": now,
        "exp": now + timedelta(seconds=settings.jwt_ttl_seconds),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def read_token(token: str) -> tuple[UUID, str]:
    """Return (user_id, role) or raise InvalidTokenError.

    Every failure mode collapses to one exception on purpose: the caller turns
    it into a 401, and distinguishing "expired" from "bad signature" in the
    response tells an attacker which of their guesses was closer.
    """
    settings = _checked_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        return UUID(payload["sub"]), payload["role"]
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise InvalidTokenError(str(exc)) from exc
