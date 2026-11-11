from fastapi import Depends, APIRouter, HTTPException,Query,status
# 类型注释
from typing import Annotated, List
from sqlalchemy.ext.asyncio import AsyncSession
from redis.asyncio import Redis
from app.models.user import UserModel
# schema
from app.schemas.position_schema import PositionCreateSchema, PositionUpdateSchema, PositionRespSchema, PositionListRespSchema
from app.schemas.response_schema import ResponseSchema
# repo service
from app.repository.position_repo import PositionRepo
from app.service import position_ser
# core
from app.core.db import get_db
from app.core.auth import create_tokens, revoke_refresh_token, refresh_access_token, get_current_user, get_super_user

router = APIRouter(prefix="/position", tags=["position"])


@router.post("/create", summary="创建职位", response_model=PositionRespSchema)
async def create_position(
    session:Annotated[AsyncSession, Depends(get_db)],
    position_data: PositionCreateSchema,
    current_user:Annotated[UserModel,Depends(get_current_user)],

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
    try:
        await position_ser.delete_position(session,position_id,current_user)
    except Exception:
        raise
    return ResponseSchema()


@router.patch("/{position_id}", summary="更新职位", response_model=PositionRespSchema)
async def update_position(
    position_id: str,
    position_data: PositionUpdateSchema,
    session: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[UserModel, Depends(get_current_user)],
):
    position = await position_ser.update_position(session, position_id, position_data, current_user)
    return {"position": position}