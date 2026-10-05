import random
import string
from redis.asyncio import Redis
from fastapi_mail import FastMail, MessageSchema
from aiosmtplib import SMTPResponseException
from loguru import logger
from app.core.mail import create_mail_instance, settings
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException, status
from app.repository.user_repo import UserRepo, DepartmentRepo
from app.schemas.user_schema import UserInviteSchema, InviteInfoSchema

# async def send_email_task(message: MessageSchema):
#     # 发送邮件
#     mail: FastMail = create_mail_instance()
#     try:
#         await mail.send_message(message)
#     except SMTPResponseException as e:
#         logger.error(f"邮件发送失败！{e}")
#
# async def send_invite_email_task(
#     email: str,
#     invite_code: str
# ):
#     # 发送邮件
#     message = MessageSchema(
#         subject="【SimpleHR】注册邀请",
#         recipients=[email],
#         body=f"您好，您的邮箱是：{email}，验证码是：{invite_code}，一天内有效。",
#         subtype="plain"
#     )
#     await send_email_task(message)


async def get_invite_code(
    invite_data: UserInviteSchema,
    session: AsyncSession,
    r:Redis,
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
    return invite_code
