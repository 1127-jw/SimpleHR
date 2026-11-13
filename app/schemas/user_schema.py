from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, ConfigDict
from typing import List
from app.models.user import UserStatus

#---------JWT---------
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
    realname: str = Field(..., description="Real name")
    phone_number: str | None = Field(None, description="Phone number")
    email: EmailStr = Field(..., description="Email address")
    avatar: str | None = Field(None, description="Avatar")
    department: DepartmentSH | None = Field(None, description="Department")
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

#---------邀请注册---------

class UserInviteSchema(BaseModel):
    email: EmailStr = Field(...,description="Email address")
    department_id: str = Field(...,description="Department ID")

class InviteInfoSchema(BaseModel):
    email: EmailStr
    department_id: str
    invite_code: str

class UserRegisterReqSchema(BaseModel):
    email: EmailStr = Field(...,description="Email address")
    invite_code: str = Field(...,max_length=6,min_length=6,description="Invite code")
    username: str = Field(...,description="Username")
    realname: str = Field(...,description="Realname")
    password: str = Field(...,max_length=15,min_length=6,description="Password")

class UserRegisterResSchema(BaseModel):
    email: EmailStr = Field(...,description="Email address")
    username: str = Field(...,description="Username")

class UserListResponseSchema(BaseModel):
    total: int = Field(...,description="Total number of users")
    items: List[UserSH] = Field(...,description="List of users")

class UserStatusUpdateSchema(BaseModel):
    email: EmailStr = Field(..., description="Email address")
    status: UserStatus = Field(..., description="new status")

class ChangePasswordSchema(BaseModel):
    old_password: str = Field(..., min_length=6, max_length=15, description="旧密码")
    new_password: str = Field(..., min_length=6, max_length=15, description="新密码")
    confirm_password: str = Field(..., min_length=6, max_length=15, description="确认新密码")

    model_config = ConfigDict(extra="forbid")

class DepartmentListRespSchema(BaseModel):
    departments: List[DepartmentSH]

class DingdingUserSchema(BaseModel):
    id: str = Field(..., description="钉钉账号在自己服务器上的id")
    nick: str = Field(..., description="在钉钉上的nickname")
    union_id: str = Field(..., description="在钉钉上的union_id")
    open_id: str = Field(..., description="在钉钉上的open_id")
    mobile: str = Field(..., description="在钉钉上的手机号")
    user_id: str = Field(..., description="在钉钉上的用户表的外键")

    model_config = ConfigDict(from_attributes=True)

class DingdingUserRespSchema(BaseModel):
    dingding_user: DingdingUserSchema | None

class AssignDepartmentSchema(BaseModel):
    hr_id: str = Field(..., description="HR的id")
    department_ids: List[str] = Field(..., description="HR负责的部门的ID列表")

class HrSchema(UserSH):
    managed_departments: List[DepartmentSH]

class HrListRespSchema(BaseModel):
    hrs: List[HrSchema]