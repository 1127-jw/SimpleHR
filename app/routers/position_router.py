
from fastapi import Depends, APIRouter, HTTPException,Query,status
# 类型注释
from typing import Annotated, List
from sqlalchemy.ext.asyncio import AsyncSession
from redis.asyncio import Redis
from app.models.user import UserModel
# schema
from app.schemas.position_schema import PositionCreateSchema, PositionRespSchema, PositionListRespSchema
from app.schemas.response_schema import ResponseSchema
# repo service
from app.repository.position_repo import PositionRepo
from app.service import position_ser
# core
from app.core.db import get_db
from app.core.auth import create_tokens, revoke_refresh_token, refresh_access_token, get_current_user, get_super_user
from app.core.config import get_settings

settings = get_settings()

router = APIRouter(prefix="/position", tags=["position"])


@router.post("/create", summary="创建职位", response_model=PositionRespSchema)
async def create_position(
    session:Annotated[AsyncSession, Depends(get_db)],
    position_data: PositionCreateSchema,
    current_user: UserModel = Depends(get_current_user),

):
    position = await position_ser.create_position(session, position_data, current_user)

    return {"position": position}

@router.get('/list', summary="职位列表", response_model=PositionListRespSchema)
async def get_position_list(
    session:Annotated[AsyncSession, Depends(get_db)],
    current_user:Annotated[UserModel,Depends(get_current_user)],
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 10,
):
    async with session.begin():
        position_repo = PositionRepo(session=session)
        positions = await position_repo.get_possition_list(current_user, page=page, size=size)
        return {"positions": positions}

@router.delete("/delete/{position_id}", summary="删除职位")
async def delete_position(
    position_id: str,
    session:Annotated[AsyncSession, Depends(get_db)],
    current_user:Annotated[UserModel,Depends(get_current_user)],
):
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
        return ResponseSchema()