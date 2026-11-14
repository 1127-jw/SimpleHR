from fastapi import HTTPException, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Annotated, Optional
from app.core.db import get_db
from app.models.user import UserModel, UserStatus
from app.schemas.user_schema import UserRegisterReqSchema, UserStatusUpdateSchema
from redis.asyncio import Redis
import random
import string
from app.core.mail import settings
from app.repository.user_repo import UserRepo, DepartmentRepo
from app.schemas.user_schema import UserInviteSchema


async def authenticate_user(
        useremail: str,
        password: str,
        session:AsyncSession
):
    async with session.begin():
        # 1 get user
        user_repo = UserRepo(session)
        user :Optional[UserModel] = await user_repo.get_by_email(useremail)
        # 2 验证user
        # 统一错误信息：区分「用户不存在」与「密码错误」会变成账号枚举入口。
        # 保持 400 是为了兼容前端 axios 拦截器（401 会触发自动刷新令牌流程）。
        if user is None or not user.check_password(password):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="邮箱或密码错误")
        if user.status != UserStatus.ACTIVE:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="账号已被禁用，请联系管理员")

        return user

async def create_user(
        register_data:UserRegisterReqSchema,
        session: AsyncSession,
        r: Redis,
):
    user_email = str(register_data.email)
    invite_code = str(register_data.invite_code)

    # 1 获取验证码
    redis_invite_code = await r.get(f"invite:{user_email}")
    if not redis_invite_code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invite_code is not exist")
    if redis_invite_code != invite_code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invite_code is not valid")
    redis_inviter_department_id = await r.get(f"inviter_department_id:{user_email}")
    # 2 开启事务
    async with session.begin():
        # 2.1 get user 检查邮箱是否已经被注册（轻量判重，不加载关联）
        user_repo = UserRepo(session)
        if await user_repo.exists_by_email(user_email):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST ,detail="user already exists")
        # 2.2 create user
        user = await user_repo.create_user({
            "username": register_data.username,
            "email": user_email,
            "password": register_data.password,
            "realname": register_data.realname,
            "department_id": redis_inviter_department_id,
        })
        return user

async def get_list_users(
        session: AsyncSession,
        user: UserModel,
        page: int = 1,
        size: int = 10,
) -> tuple[list[UserModel], int]:
    """返回 (当前页用户, 总数)。"""
    async with session.begin():
        user_repo = UserRepo(session)
        return await user_repo.get_user_list(page, size, user.department_id)

async def update_user_status(
        session: Annotated[AsyncSession, Depends(get_db)],
        status_data: UserStatusUpdateSchema,

):
    async with session.begin():
        user_repo = UserRepo(session)
        user: UserModel|None = await user_repo.get_by_email(str(status_data.email))
        if not user:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="该员工不存在！")
        if user.is_superuser:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不能修改超级用户的状态！")
        user.status = status_data.status
        return user





async def get_invite_code(
    invite_data: UserInviteSchema,
    session: AsyncSession,
    r:Redis,
    user_department_id: str,
):
    async with session.begin():
        user_repo = UserRepo(session)
        user = await user_repo.get_by_email(str(invite_data.email))
        if user:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="该邮箱已存在！")
        department_repo = DepartmentRepo(session)
        department = await department_repo.get_by_id(department_id=invite_data.department_id)
        if not department:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="该部门不存在！")
    # 生成6位数字验证码
    invite_code = "".join(random.sample(string.digits, 6))
    # 存储到redis缓存中
    await r.setex(
        f"invite:{invite_data.email}",
        int(settings.INVITE_EXPIRE_MINUTES*60),
        invite_code,
    )
    await r.setex(
        f"inviter_department_id:{invite_data.email}",
        int(settings.INVITE_EXPIRE_MINUTES*60),
        user_department_id,
    )

    return invite_code