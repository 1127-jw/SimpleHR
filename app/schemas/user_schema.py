from pydantic import BaseModel, EmailStr, Field, ConfigDict


class UserLoginSH(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr = Field(...,description="Email address")
    password: str = Field(...,description="Password",min_length=6,max_length=15)

class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str