"""Part B response used by the operator account list."""

from app.schemas.user import UserResponse


class AdminUserResponse(UserResponse):
    job_count: int
