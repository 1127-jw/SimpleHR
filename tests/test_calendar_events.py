"""日程接口测试：取日程的解析/排序，以及未绑定钉钉时的错误映射。"""
from __future__ import annotations

import sys
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.service.dingtalk_ser as ds
from app.core.exceptions import DingTalkServiceError

# app/routers/__init__.py 把包属性 `calendar_router` 覆盖成了 APIRouter 对象，
# `import ... as` 取到的是路由实例而非模块，因此先触发包导入、再绕过包属性
# 取真正加载好的模块（同 test_candidate_ownership.py 的写法）。
from app.routers import calendar_router as _pkg_attr  # noqa: F401  仅确保子模块已导入

cr = sys.modules["app.routers.calendar_router"]


async def test_get_user_calendar_events_parses_and_sorts(monkeypatch):
    """解析字段、按开始时间升序、全天日程识别、脏数据跳过。"""

    async def fake_token(user_id):
        return "union-1", {"access_token": "tk"}

    captured: dict = {}

    async def fake_list(*, union_id, access_token, time_min, time_max):
        captured.update(
            union_id=union_id, access_token=access_token,
            time_min=time_min, time_max=time_max,
        )
        return [
            {
                "eventId": "b", "summary": "下午的会",
                "start": {"dateTime": "2026-11-20T14:00:00+08:00"},
                "end": {"dateTime": "2026-11-20T15:00:00+08:00"},
                "location": {"displayName": "会议室 A"},
            },
            {
                "eventId": "a", "summary": "上午的会",
                "start": {"dateTime": "2026-11-20T10:00:00+08:00"},
                "end": {"dateTime": "2026-11-20T11:00:00+08:00"},
            },
            {
                # 全天日程只有 date 字段
                "eventId": "c", "summary": "全天",
                "start": {"date": "2026-11-21"}, "end": {"date": "2026-11-22"},
            },
            {"eventId": "d", "summary": "缺时间"},   # 脏数据，应跳过
        ]

    monkeypatch.setattr(ds, "get_union_id_and_access_token_by_user_id", fake_token)
    monkeypatch.setattr(ds, "get_calendar_list", fake_list)

    items = await ds.get_user_calendar_events("user-1", days=7)

    assert [i["event_id"] for i in items] == ["a", "b", "c"], "应按开始时间升序"
    assert items[0]["title"] == "上午的会"
    assert items[0]["start"].startswith("2026-11-20T10:00:00")
    assert items[1]["location"] == "会议室 A"
    assert items[2]["is_all_day"] is True
    assert captured["union_id"] == "union-1"
    assert captured["time_max"] - captured["time_min"] == timedelta(days=7)


async def test_calendar_events_maps_dingtalk_error_to_400(monkeypatch):
    """未绑定钉钉 → 400，而不是被全局处理器映射成 502。"""

    async def boom(user_id, *, days=7):
        raise DingTalkServiceError("用户 user-1 未绑定钉钉账号")

    monkeypatch.setattr(cr, "get_user_calendar_events", boom)

    with pytest.raises(HTTPException) as exc:
        await cr.list_my_events(days=7, current_user=SimpleNamespace(id="user-1"))

    assert exc.value.status_code == 400
    assert "未绑定钉钉" in str(exc.value.detail)


async def test_calendar_events_empty(monkeypatch):
    """日历为空时服务返回空列表，接口给出 total=0。"""

    async def fake_token(user_id):
        return "union-1", {"access_token": "tk"}

    async def fake_list(*, union_id, access_token, time_min, time_max):
        return []

    async def fake_events(user_id, *, days=7):
        return []

    monkeypatch.setattr(ds, "get_union_id_and_access_token_by_user_id", fake_token)
    monkeypatch.setattr(ds, "get_calendar_list", fake_list)
    monkeypatch.setattr(cr, "get_user_calendar_events", fake_events)

    assert await ds.get_user_calendar_events("user-1", days=7) == []

    resp = await cr.list_my_events(days=7, current_user=SimpleNamespace(id="user-1"))
    assert resp == {"items": [], "total": 0}
