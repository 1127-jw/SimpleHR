"""日程相关 schema。

数据来自钉钉日历（``app/service/dingtalk_ser.py``），时间统一为
ISO-8601 字符串 + ``+08:00`` 偏移（见 ``app/core/time.py``）。
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class CalendarEventSchema(BaseModel):
    """一条日程。"""

    event_id: str = Field(default="", description="钉钉事件 ID")
    title: str = Field(default="", description="日程标题")
    start: str = Field(default="", description="开始时间（ISO-8601，北京时间）")
    end: str = Field(default="", description="结束时间（ISO-8601，北京时间）")
    location: str = Field(default="", description="地点，可能为空")
    is_all_day: bool = Field(default=False, description="是否全天日程")


class CalendarEventsRespSchema(BaseModel):
    """日程列表响应。"""

    items: list[CalendarEventSchema] = Field(default_factory=list)
    total: int = Field(default=0)
