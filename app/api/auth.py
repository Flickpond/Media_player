"""Register, log in, log out, and report who you are.

The token is a signed JWT delivered in an HttpOnly cookie. That is deliberate
and has a consequence worth knowing before changing anything here: JavaScript
cannot read the cookie, which is the point, but it also means the frontend
cannot decode the token to find out who it is. That is what `GET /auth/me` is
for, and it is why logout has to be a server endpoint -- a script cannot delete
an HttpOnly cookie itself.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.config import get_settings
from app.database import get_session
from app.errors import ApiUnauthorizedError
from app.repositories.users import (
    EmailAlreadyRegisteredError,
    create_user,
    get_user_by_email,
)
from app.schemas.user import Credentials, UserResponse
from app.services.security import COOKIE_NAME, hash_password, issue_token, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])
SessionDependency = Annotated[AsyncSession, Depends(get_session)]


def _set_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.jwt_ttl_seconds,
        httponly=True,
        # Strict rather than Lax: nothing links into this app from another
        # site, so Strict costs nothing and closes CSRF outright.
        samesite="strict",
        # Inert over plain HTTP, set anyway so it is correct the moment the
        # deployment is behind TLS -- which it now is.
        secure=True,
        path="/",
    )


@router.post("/register", status_code=status.HTTP_201_CREATED, response_model=UserResponse)
async def register(credentials: Credentials, session: SessionDependency, response: Response):
    try:
        user = await create_user(
            session,
            email=credentials.email,
            password_hash=hash_password(credentials.password),
        )
    except EmailAlreadyRegisteredError:
        # 409 rather than a generic error: the caller is the person choosing
        # the address, and they need to know it is taken. This does leak that
        # the address exists, which is unavoidable for a self-service signup.
        return Response(status_code=status.HTTP_409_CONFLICT)

    _set_cookie(response, issue_token(user_id=user.id, role=user.role))
    return user


@router.post("/login", response_model=UserResponse)
async def login(credentials: Credentials, session: SessionDependency, response: Response):
    user = await get_user_by_email(session, credentials.email)
    # Deliberately one branch and one message. An unknown address and a wrong
    # password must be indistinguishable, or the endpoint becomes a way to
    # enumerate who has an account. verify_password is still called against a
    # dummy hash when the user is missing so the timing does not give it away
    # either.
    candidate = user.password_hash if user else "$2b$12$" + "." * 53
    if not verify_password(credentials.password, candidate) or user is None:
        raise ApiUnauthorizedError("invalid email or password")

    _set_cookie(response, issue_token(user_id=user.id, role=user.role))
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response) -> Response:
    """Clear the cookie.

    Worth being honest about what this does: the JWT itself stays valid until
    it expires. This removes the browser's copy, which is all a stateless token
    allows. The short lifetime in `jwt_ttl_seconds` is the real bound.
    """
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict", secure=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserResponse)
async def me(user: CurrentUser):
    """The frontend cannot read an HttpOnly cookie, so it asks."""
    return user
