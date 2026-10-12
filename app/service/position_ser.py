from app.models import PositionModel
from app.repository.position_repo import PositionRepo
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException, status

from app.models.user import UserModel


async def create_position(
        session:AsyncSession,
        position_data,
        current_user
) -> PositionModel:
    async with session.begin():
        position_repo = PositionRepo(session=session)
        position_dict = position_data.model_dump()
        position_dict['creator_id'] = current_user.id
        position_dict['department_id'] = current_user.department.id
        position = await position_repo.create_position(position_dict)
        return position

async def delete_position(
        session:AsyncSession,
        position_id:str,
        current_user:UserModel

) :
    async with session.begin():
        position_repo = PositionRepo(session=session)
        position = await position_repo.get_by_id(position_id)
        if not position:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="该职位不存在！")
        # 如果是superuser，那么可以直接删除；否则就只能是所属部门的人才能删除
        if (not current_user.is_superuser) and (position.department_id != current_user.department.id):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="没有权限执行操作！")

        # 执行删除操作
        try:
            await position_repo.delete_position(position_id)
        except Exception as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="职位删除失败！")