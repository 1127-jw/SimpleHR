from fastapi import APIRouter, HTTPException, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Annotated, Optional
from core.db import get_db
from app.repository.user_repo import UserRepo
from app.models.user import UserModel
from core.redis_client import get_redis
from schemas.user_schema import UserLoginSH, RefreshRequest, LogoutRequest
from app.core.auth import create_tokens, revoke_refresh_token, refresh_access_token, get_current_user
router = APIRouter(prefix="/user", tags=["user"])

@router.get("/token",summary="登录")
async def login(
    login_data:UserLoginSH,
    session:Annotated[AsyncSession, Depends(get_db)]
):
    #开启事务
    async with session.begin():
        #1 get user
        user_repo = UserRepo()
        user:Optional[UserModel] = await user_repo.get_by_email(str(login_data.email))
        #2 验证user
        if not user:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,detail="no such user")
        if not user.check_password(login_data.password):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,detail="wrong password")
        r = await get_redis()
        tokens = await create_tokens(login_data.email,r)

        return {
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "token_type": tokens["token_type"],
        }


@router.post("/refresh")
async def refresh(body: RefreshRequest):
    r = await get_redis()
    return await refresh_access_token(body.refresh_token, r)


@router.post("/logout")
async def logout(body: LogoutRequest):
    r = await get_redis()
    await revoke_refresh_token(body.refresh_token, r)
    return {"detail": "Logged out"}


@router.get("/me")
async def me(user: dict = Depends(get_current_user)):
    return user