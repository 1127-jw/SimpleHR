import random
import string
from redis.asyncio import Redis
from app.core.mail import settings
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException, status
from app.repository.user_repo import UserRepo, DepartmentRepo
from app.schemas.user_schema import UserInviteSchema


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
