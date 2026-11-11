from typing import cast

from . import BaseRepo
from app.models.user import UserModel
from sqlalchemy import select, delete
from typing import Sequence
from sqlalchemy.orm import selectinload
from app.models.position import PositionModel


def position_eager_options():
    """职位响应（``PositionSchema``）所需的关联预加载。

    替代原先 ``lazy="joined"`` 的全局加载：只在真正需要序列化职位的
    查询路径上展开 ``creator``（含其 department）与 ``department``。
    """
    return (
        selectinload(PositionModel.creator).selectinload(UserModel.department),
        selectinload(PositionModel.department),
    )


class PositionRepo(BaseRepo):
    async def create_position(self, position_data: dict) -> PositionModel:
        position = PositionModel(**position_data)
        self.session.add(position)
        await self.session.flush()
        # 重新查询以带上响应所需的关联
        return cast(PositionModel, await self.get_by_id(position.id))

    async def get_possition_list(self,
        user: UserModel,
        page: int = 1,
        size: int = 10
    ) -> Sequence[PositionModel]:
        stmt = select(PositionModel)
        # 1. 如果是hr，那么只返回该hr负责的部门的职位
        # 2. 如果是其他部门的员工，那么只要返回该员工所在部门的职位就可以了
        # 3. 如果是superuser，那么就没有条件
        if user.is_hr and (not user.is_superuser):
            department_ids = [d.id for d in user.managed_departments]
            stmt = stmt.where(PositionModel.department_id.in_(department_ids))
        elif (not user.is_hr) and (not user.is_superuser):
            stmt = stmt.where(PositionModel.department_id == user.department_id)
        # 分页
        limit = size
        offset = (page - 1) * size
        stmt = (
            stmt.options(*position_eager_options())
            .limit(limit)
            .offset(offset)
            .order_by(PositionModel.created_at.desc())
        )
        positions = (await self.session.scalars(stmt)).all()
        return positions

    async def get_by_id(self, position_id: str) -> PositionModel | None:
        return await self.session.scalar(
            select(PositionModel)
            .where(PositionModel.id == position_id)
            .options(*position_eager_options())
        )

    async def delete_position(self, position_id: str):
        stmt = delete(PositionModel).where(PositionModel.id == position_id)
        await self.session.execute(stmt)

    async def update_position(self, position_id: str, position_dict: dict) -> PositionModel | None:
        """更新职位信息。

        Returns:
            更新后的 PositionModel，如果职位不存在返回 None。
        """
        position = await self.get_by_id(position_id)
        if position is None:
            return None

        for key, value in position_dict.items():
            if value is not None:
                setattr(position, key, value)

        await self.session.flush()
        # 重新查询以带上响应所需的关联（refresh 不会加载关联对象）
        return await self.get_by_id(position_id)
