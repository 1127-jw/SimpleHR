from fastapi import HTTPException, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Annotated, Optional
from app.core.db import get_db
from app.repository.user_repo import UserRepo
from app.models.user import UserModel, UserStatus


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
        if not user:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST ,detail="no such user")
        if not user.check_password(password):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST ,detail="wrong password")
        if user.status != UserStatus.ACTIVE:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST ,detail="inactive user")

        return user