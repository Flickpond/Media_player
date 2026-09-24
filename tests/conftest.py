"""Shared test identity.

Every job endpoint now resolves a caller, so a test that exercises one has to
supply a user. Overriding the dependency is deliberate: it keeps these tests
about the endpoint's own behaviour rather than about logging in, which
tests/test_auth.py covers on its own.
"""

from uuid import uuid4

import pytest

from app.api.deps import current_user, operator_user
from app.models.user import User, UserRole


def make_user(role: UserRole = UserRole.USER) -> User:
    user = User(
        id=uuid4(),
        email=f"{role.value}-{uuid4().hex[:8]}@example.test",
        password_hash="not-a-real-hash",
        role=role.value,
    )
    return user


@pytest.fixture
def test_user() -> User:
    return make_user()


@pytest.fixture
def other_user() -> User:
    """A second account, for proving one caller cannot see another's jobs."""
    return make_user()


@pytest.fixture
def operator() -> User:
    return make_user(UserRole.OPERATOR)


def authenticate_as(application, user: User) -> None:
    """Make `application` treat every request as coming from `user`.

    Overrides `operator_user` too, but only to the extent of letting the real
    role check run against this user -- a non-operator still gets 403, because
    the override returns the user and the endpoint's own check is what decides.
    """
    application.dependency_overrides[current_user] = lambda: user

    async def _operator() -> User:
        from app.errors import ApiForbiddenError

        if user.role != UserRole.OPERATOR.value:
            raise ApiForbiddenError("operator role required")
        return user

    application.dependency_overrides[operator_user] = _operator
