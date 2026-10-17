from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.user import UserRole


class Credentials(BaseModel):
    email: EmailStr
    # 72 is bcrypt's hard limit -- anything past it is silently ignored by the
    # algorithm, so it is rejected here rather than quietly truncated.
    password: str = Field(min_length=8, max_length=72)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    role: UserRole
