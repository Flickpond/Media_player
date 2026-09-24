"""Who is asking.

One dependency, so there is a single place to audit the answer. Everything
that needs a caller takes `CurrentUser`; everything that needs an operator
takes `OperatorUser`.
"""

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.errors import ApiForbiddenError, ApiUnauthorizedError
from app.models.user import User, UserRole
from app.repositories.users import get_user
from app.services.security import COOKIE_NAME, InvalidTokenError, read_token

SessionDependency = Annotated[AsyncSession, Depends(get_session)]


async def current_user(request: Request, session: SessionDependency) -> User:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise ApiUnauthorizedError("not authenticated")

    try:
        user_id, _role = read_token(token)
    except InvalidTokenError as exc:
        # Malformed, expired and wrongly-signed all land here with the same
        # response. Telling a caller which one it was tells them how close
        # their guess came.
        raise ApiUnauthorizedError("not authenticated") from exc

    # The role is read from the database rather than trusted from the token.
    # A JWT cannot be revoked, so a claim minted before a demotion would still
    # say `operator` until it expired.
    user = await get_user(session, user_id)
    if user is None:
        raise ApiUnauthorizedError("not authenticated")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


async def operator_user(user: CurrentUser) -> User:
    if user.role != UserRole.OPERATOR.value:
        raise ApiForbiddenError("operator role required")
    return user


OperatorUser = Annotated[User, Depends(operator_user)]
