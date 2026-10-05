from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, ConfigDict

from app.models import DepartmentModel
from app.models.user import UserStatus


class UserLoginReqSH(BaseModel):
    email: EmailStr = Field(...,description="Email address")
    password: str = Field(...,description="Password",min_length=6,max_length=15)

    model_config = ConfigDict(extra="forbid")

class DepartmentSH(BaseModel):
    id: str = Field(...,description="Department ID")
    name: str = Field(...,description="Department name")
    description: str | None = Field(...,description="Department description")

    model_config = ConfigDict(from_attributes=True)

class UserSH(BaseModel):
    id: str = Field(...,description="User ID")
    username: str = Field(...,description="Username")
    phone_number: str | None = Field(None, description="Phone number")
    email: EmailStr = Field(..., description="Email address")
    avatar: str | None = Field(None, description="Avatar")
    department: DepartmentSH = Field(...,description="Department")
    status: UserStatus = Field(...,description="Status")
    is_superuser: bool = Field(...,description="Superuser")
    is_hr: bool = Field(...,description="HR")
    created_at: datetime = Field(...,description="Created at")

    model_config = ConfigDict(from_attributes=True)


class UserLoginResSH(BaseModel):
    access_token: str = Field(...,description="access token")
    refresh_token: str = Field(...,description="refresh token")
    token_type: str = Field(...,description="token type")
    user: UserSH


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str