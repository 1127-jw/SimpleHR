"""日程接口：把当前登录用户的钉钉日历暴露给前端。

复用 ``app/service/dingtalk_ser.py`` 里的取日程能力（``get_calendar_list`` +
``parse_event_boundary``），这里只做鉴权、参数校验与响应整形。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import get_current_user
from app.core.exceptions import DingTalkServiceError
from app.models.user import UserModel
from app.schemas.calendar_schema import CalendarEventsRespSchema
from app.service.dingtalk_ser import get_user_calendar_events

router = APIRouter(prefix="/calendar", tags=["calendar"])


@router.get("/events", summary="我的日程列表", response_model=CalendarEventsRespSchema)
async def list_my_events(
    days: int = Query(7, ge=1, le=30, description="从当前时间起往后数多少天"),
    current_user: UserModel = Depends(get_current_user),
):
    """当前登录用户未来 ``days`` 天的日程，按开始时间升序。

    未绑定钉钉账号时返回 400（``DingTalkServiceError`` 由全局处理器映射成 502，
    对前端不友好，这里就地转成带说明的 400）。
    """
    try:
        items = await get_user_calendar_events(current_user.id, days=days)
    except DingTalkServiceError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return {"items": items, "total": len(items)}
