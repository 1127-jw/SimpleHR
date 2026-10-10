from app.models import PositionModel
from app.repository.position_repo import PositionRepo
from sqlalchemy.ext.asyncio import AsyncSession

async def create_position(session:AsyncSession,position_data, current_user) -> PositionModel:
    async with session.begin():
        position_repo = PositionRepo(session=session)
        position_dict = position_data.model_dump()
        position_dict['creator_id'] = current_user.id
        position_dict['department_id'] = current_user.department.id
        position = await position_repo.create_position(position_dict)
        return position
