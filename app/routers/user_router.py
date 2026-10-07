from fastapi import APIRouter, HTTPException, Depends, status, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Annotated, List
from app.core.db import get_db
from app.core.redis_client import get_redis
from app.schemas.user_schema import UserLoginReqSH, UserLoginResSH, RefreshRequest, LogoutRequest, UserInviteSchema, \
    UserRegisterReqSchema, UserRegisterResSchema, UserListResponseSchema, UserStatusUpdateSchema
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
    user: UserModel = Depends(get_super_user),
):
    invite_code = await get_invite_code(invite_data,session,r,user.department_id)
    background_tasks.add_task(send_invite_email_task, str(invite_data.email), invite_code)
    return ResponseSchema()

@router.post("/register",summary="注册", response_model=UserRegisterResSchema)
async def register(
    register_data: UserRegisterReqSchema,
    session: Annotated[AsyncSession, Depends(get_db)],
    r: Redis = Depends(get_redis),

):
    user = await auth_ser.create_user(register_data,session,r)

    return {
        "email": user.email,
        "username": user.username,
    }

@router.get("/list",summary="获取员工列表",response_model=UserListResponseSchema)
async def list_users(
        session: Annotated[AsyncSession, Depends(get_db)],
        page: int = 1,
        size: int = 10,
        user: UserModel = Depends(get_current_user),
):
    user_list =await auth_ser.get_list_users(session,user,page,size)
    return {
        "total": len(user_list),
        "items": user_list[(page - 1) * size : page * size],
    }

@router.patch("/status/update",summary="修改员工状态", response_model=ResponseSchema)
async def update_status(
    status_data: UserStatusUpdateSchema,
    session: Annotated[AsyncSession, Depends(get_db)],
    _: UserModel = Depends(get_super_user),
):
    await  auth_ser.update_user_status(session,status_data)
    return ResponseSchema()
