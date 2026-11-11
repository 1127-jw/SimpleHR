from app.models import PositionModel
from app.repository.position_repo import PositionRepo
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException, status

from app.models.user import UserModel


def _require_department_id(current_user: UserModel) -> str:
    """取当前用户的部门 id；未分配部门时给出明确的业务错误而不是 AttributeError。"""
    if not current_user.department_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="当前用户未分配部门，无法执行该操作！",
        )
    return current_user.department_id


async def create_position(
        session:AsyncSession,
        position_data,
        current_user
) -> PositionModel:
    async with session.begin():
        position_repo = PositionRepo(session=session)
        position_dict = position_data.model_dump()
        position_dict['creator_id'] = current_user.id
        position_dict['department_id'] = _require_department_id(current_user)
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
        if (not current_user.is_superuser) and (position.department_id != _require_department_id(current_user)):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="没有权限执行操作！")

        # 执行删除操作
        try:
            await position_repo.delete_position(position_id)
        except Exception as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="职位删除失败！")


async def update_position(
    session: AsyncSession,
    position_id: str,
    position_data,
    current_user: UserModel,
) -> PositionModel:
    """更新职位信息。

    Args:
        session: 数据库会话。
        position_id: 职位 ID。
        position_data: 更新数据（PositionUpdateSchema）。
        current_user: 当前用户。

    Returns:
        更新后的 PositionModel。

    Raises:
        HTTPException: 职位不存在或权限不足。
    """
    async with session.begin():
        position_repo = PositionRepo(session=session)
        position = await position_repo.get_by_id(position_id)
        if not position:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="该职位不存在！")

        # 权限校验：superuser 或 所属部门的人
        if (not current_user.is_superuser) and (position.department_id != _require_department_id(current_user)):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="没有权限执行操作！")

        # 过滤掉 None 值，只更新提供的字段
        update_dict = {k: v for k, v in position_data.model_dump().items() if v is not None}

        updated_position = await position_repo.update_position(position_id, update_dict)
        return updated_position