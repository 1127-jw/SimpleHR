from fastapi import APIRouter, HTTPException, Depends, status, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Annotated
from app.core.db import get_db
from app.core.redis_client import get_redis
from app.schemas.user_schema import UserLoginReqSH, UserLoginResSH, RefreshRequest, LogoutRequest, UserInviteSchema
from app.schemas.response_schema import ResponseSchema
from app.core.auth import create_tokens, revoke_refresh_token, refresh_access_token, get_current_user, get_super_user
from app.service import auth_ser
from redis.asyncio import Redis
from app.models.user import UserModel
from app.service.email_ser import get_invite_code
from app.tasks.email_tasks import send_invite_email_task

router = APIRouter(prefix="/user", tags=["user"])

@router.post("/token",summary="登录",response_model=UserLoginResSH)
async def login(
    login_data:UserLoginReqSH,
    session:Annotated[AsyncSession, Depends(get_db)],
    r: Redis = Depends(get_redis),
):
    user = await auth_ser.authenticate_user(login_data.email,login_data.password,session)
    tokens = await create_tokens(login_data.email,r)

    return {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "token_type": tokens["token_type"],
        "user": user
    }

@router.post("/refresh")
async def refresh(
        body: RefreshRequest,
        r: Redis = Depends(get_redis),
):

    return await refresh_access_token(body.refresh_token, r)


@router.post("/logout")
async def logout(
        body: LogoutRequest,
        r: Redis = Depends(get_redis),
):
    await revoke_refresh_token(body.refresh_token, r)
    return {"detail": "Logged out"}


@router.get("/me")
async def me(user: dict = Depends(get_current_user)):
    return user

@router.post("/invite", summary="邀请用户", response_model=ResponseSchema)
async def invite_user(
    invite_data: UserInviteSchema,
    background_tasks: BackgroundTasks,
    session:Annotated[AsyncSession, Depends(get_db)],
    r: Redis = Depends(get_redis),
    _: UserModel = Depends(get_super_user),
):
    invite_code = await get_invite_code(invite_data,session,r)
    background_tasks.add_task(send_invite_email_task, str(invite_data.email), invite_code)
    return ResponseSchema()