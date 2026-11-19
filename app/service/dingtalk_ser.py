from fastapi import HTTPException

from alibabacloud_dingtalk.oauth2_1_0.client import Client as dingtalkoauth2_1_0Client
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_dingtalk.oauth2_1_0 import models as dingtalkoauth_2__1__0_models

from datetime import datetime, timedelta
from urllib.parse import quote, urljoin
from redis.asyncio import Redis
import logging
from typing import cast, Any
from app.repository.user_repo import UserRepo
from app.core.config import get_settings
from app.core.db import AsyncSessionFactory
from app.core.http_client import get_http_client
from app.core.redis_client import get_redis
from app.core.exceptions import DingTalkServiceError
from app.models import DingdingUserModel
from utils.available_time import find_available_slot
from app.core.time import (
    BEIJING_TZ_NAME,
    from_iso8601,
    from_naive_beijing,
    now_beijing,
    to_iso8601,
)

logger = logging.getLogger(__name__)

settings = get_settings()



DINGTALK_BASE = "https://api.dingtalk.com"

_dingtalk_oauth_client: dingtalkoauth2_1_0Client | None = None

def get_dingtalk_oauth_client() -> dingtalkoauth2_1_0Client:
    global _dingtalk_oauth_client
    if _dingtalk_oauth_client is None:
        config = open_api_models.Config()
        config.protocol = "https"
        config.region_id = "central"
        _dingtalk_oauth_client = dingtalkoauth2_1_0Client(config)
    return _dingtalk_oauth_client

async def call_dingtalk_service(
        r:Redis,
        state: str,
        code: str | None = None,
) -> dict:
    client = get_dingtalk_oauth_client()


    request = dingtalkoauth_2__1__0_models.GetUserTokenRequest(
        client_id=settings.DINGTALK_CLIENT_ID,
        client_secret=settings.DINGTALK_CLIENT_SECRET,
        code=code,
        grant_type="authorization_code",
    )

    resp = client.get_user_token(request)
    access_token = resp.body.access_token
    refresh_token = resp.body.refresh_token
    if not access_token:
        raise HTTPException(status_code=502, detail="钉钉返回的 access_token 为空")
    if not refresh_token:
        raise HTTPException(status_code=502, detail="钉钉返回的 refreshToken 为空")

    ac_cache_key = f"dingtalk:user_actoken:{state}"
    re_cache_key = f"dingtalk:user_retoken:{state}"

    await r.set(ac_cache_key, access_token, ex=7200)
    await r.set(re_cache_key, refresh_token, ex=30*24*60*60)
    return {
        "access_token": access_token,
        "state": state,
    }


async def refresh_user_access_token(
        r: Redis,
        state: str,
        refresh_token: str,
) -> dict:
    """刷新用户级 access_token。

    返回 (access_token, refresh_token, expire_in)
    """
    # 复用全局 client（见 app/core/http_client.py）
    resp = await get_http_client().post(
        f"{DINGTALK_BASE}/v1.0/oauth2/userAccessToken",
        json={
            "clientId": settings.DINGTALK_CLIENT_ID,
            "clientSecret": settings.DINGTALK_CLIENT_SECRET,
            "grantType": "refresh_token",
            "refreshToken": refresh_token,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    access_token = data["accessToken"]
    refresh_token = data["refreshToken"]

    ac_cache_key = f"dingtalk:user_actoken:{state}"
    re_cache_key = f"dingtalk:user_retoken:{state}"

    await r.set(ac_cache_key, access_token, ex=7200)
    await r.set(re_cache_key, refresh_token, ex=30 * 24 * 60 * 60)

    return {
        "access_token": access_token,
        "state": state,
    }


async def get_user_access_token(
        state: str,
        r: Redis
) -> dict:
    """
    通过state获取缓存中的access_token

    """
    ac_cache_key = f"dingtalk:user_actoken:{state}"
    re_cache_key = f"dingtalk:user_retoken:{state}"
    ac_token = await r.get(ac_cache_key)
    # 注意不要写成 str(await r.get(...))：key 不存在时它得到字符串 "None"，
    # 是一个真值，会被当成合法 refresh_token 发出去，最后变成钉钉侧一个
    # 莫名其妙的 400，而不是「请重新授权」。
    re_token = await r.get(re_cache_key)
    if not ac_token:
        if not re_token:
            error_message = f"{state}用户钉钉授权已失效且无 refresh_token，请重新授权"
            logger.error(error_message)
            raise HTTPException(status_code=400, detail=error_message)
        try:
            access_token_info = await refresh_user_access_token(r, state, re_token)
        except Exception as e:
            error_message = f"{state}用户钉钉授权已过期,错误代码为{e}"
            logger.error(error_message)
            raise HTTPException(status_code=400, detail=error_message)
        return access_token_info
    return {
        "access_token": ac_token,
        "state": state
    }

async def get_union_id_and_access_token_by_user_id(interviewer_id: str) -> tuple[str, dict]:
    """获取面试官钉钉账户的 access_token 和 union_id。

    Returns:
        (union_id, access_token_info) 元组。

    Raises:
        DingTalkServiceError: 未绑定钉钉账号或 token 获取失败。
    """
    async with AsyncSessionFactory() as session:
        async with session.begin():
            user_repo = UserRepo(session)
            dingding_user: DingdingUserModel | None = await user_repo.get_dingding_user(
                user_id=interviewer_id
            )
            if not dingding_user:
                raise DingTalkServiceError(
                    f"用户 {interviewer_id} 未绑定钉钉账号"
                )
            union_id = dingding_user.union_id

    try:
        r = get_redis()
        access_token_info = await get_user_access_token(interviewer_id, r)
    except Exception as e:
        logger.error("获取钉钉 access_token 失败: %s", e)
        raise DingTalkServiceError(
            f"获取钉钉 access_token 失败: {e}"
        ) from e

    return union_id, access_token_info





async def get_dingtalk_user(session,state,access_token: str, union_id: str="me"):
    resp = await get_http_client().get(
        f"{DINGTALK_BASE}/v1.0/contact/users/{union_id}",
        headers={
            "x-acs-dingtalk-access-token": access_token,
        },
    )
    resp.raise_for_status()
    payload = resp.json()
    nick = payload["nick"]
    mobile = payload["mobile"]
    open_id = payload["openId"]
    union_id = payload["unionId"]

    async with session.begin():
        # 2.1 get user 检查邮箱是否已经被注册
        user_repo = UserRepo(session)
        await user_repo.set_dingding_user(
            user_id=state,
            dingding_user_data={
                "nick": nick,
                "mobile": mobile,
                "open_id": open_id,
                "union_id": union_id,
            }
        )

    return f"/user/dingtalk/authorize/success?nick={nick}"


#创建日程日期的查询url
def build_get_calendar_list_url(union_id: str, time_min: datetime, time_max: datetime):
    # 将日期转化为ISO-8601的date-time格式
    time_min_iso8601: str = to_iso8601(time_min)
    time_max_iso8601: str = to_iso8601(time_max)

    # 对时间字符串进行 URL 编码，防止 '+' 被识别为空格
    time_min_encoded = quote(time_min_iso8601)
    time_max_encoded = quote(time_max_iso8601)

    path = f"/v1.0/calendar/users/{union_id}/calendars/primary/events?timeMin={time_min_encoded}&timeMax={time_max_encoded}"
    return urljoin(DINGTALK_BASE, path)

#获取日程列表
async def get_calendar_list(
        union_id: str,
        access_token: str,
        time_min: datetime,
        time_max: datetime
)->list[dict[str, Any]]:
    url = build_get_calendar_list_url(union_id, time_min, time_max)
    headers = {
        "x-acs-dingtalk-access-token": access_token
    }
    calendar_resp = await get_http_client().get(url, headers=headers)
    calendar_resp.raise_for_status()
    calendar_data = calendar_resp.json() or {}
    # 返回值必须恒为列表：钉钉在「该时间段没有任何日程」时可能不返回 events 字段，
    # 直接透传 None 会让调用方的 ``for event in events`` 抛 TypeError，
    # 再被外层兜成 DingTalkServiceError —— 一位日历全空的面试官就能让邀请流程失败。
    return calendar_data.get("events") or []


def parse_event_boundary(event: dict[str, Any], which: str) -> datetime | None:
    """解析日程的起止时间，兼容全天日程。
    钉钉的定时日程给 ``{"dateTime": "...", "timeZone": "..."}``，
    全天日程（请假、年假等）只给 ``{"date": "YYYY-MM-DD"}``。
    """
    boundary = event.get(which) or {}
    raw = boundary.get("dateTime")
    if raw:
        return from_iso8601(raw)
    raw_date = boundary.get("date")
    if raw_date:
        # 全天日程按北京时间整天处理
        return from_naive_beijing(datetime.fromisoformat(str(raw_date)[:10]))
    logger.warning("日程缺少 %s 时间字段，已跳过: %s", which, boundary)
    return None


async def get_user_calendar_events(
    user_id: str,
    *,
    days: int = 7,
) -> list[dict[str, Any]]:
    """取某个用户（需已绑定钉钉）**未来 ``days`` 天**的日程。

    复用 ``get_union_id_and_access_token_by_user_id`` + ``get_calendar_list``，
    与 ``get_user_available_slots`` 走同一条鉴权路径，不重复实现 token 逻辑。

    Args:
        user_id: 钉钉绑定记录所属的用户 ID（传当前登录用户即可）。
        days: 从**当前时间**起往后数多少天。

    Returns:
        按开始时间升序的日程字典列表，字段见 ``CalendarEventSchema``。
        缺时间字段的脏日程直接跳过——一条坏数据不该让整页日程空白。

    Raises:
        DingTalkServiceError: 未绑定钉钉账号，或 access_token 获取失败。
    """
    union_id, access_token_info = await get_union_id_and_access_token_by_user_id(user_id)
    access_token = cast(str, access_token_info.get("access_token"))

    now = now_beijing()
    events: list[dict[str, Any]] = await get_calendar_list(
        union_id=union_id,
        access_token=access_token,
        time_min=now,
        time_max=now + timedelta(days=days),
    )

    items: list[dict[str, Any]] = []
    for event in events:
        start_dt = parse_event_boundary(event, "start")
        end_dt = parse_event_boundary(event, "end")
        if start_dt is None or end_dt is None:
            logger.warning("[calendar] 日程缺时间字段，已跳过 | eventId=%s", event.get("eventId"))
            continue
        raw_start = event.get("start") or {}
        items.append({
            "event_id": event.get("eventId") or "",
            "title": event.get("summary") or "（无标题）",
            "start": to_iso8601(start_dt),
            "end": to_iso8601(end_dt),
            "location": (event.get("location") or {}).get("displayName") or "",
            "is_all_day": "date" in raw_start,
        })

    items.sort(key=lambda x: str(x["start"]))
    return items


#创建日程的url
def build_create_calendar_url(union_id: str):
    path = f"/v1.0/calendar/users/{union_id}/calendars/primary/events"
    return urljoin(DINGTALK_BASE, path)

#创建日程
async def create_calendar(union_id: str, access_token: str, summary: str, start_datetime: datetime, end_datetime: datetime):
    url = build_create_calendar_url(union_id)
    headers = {
        "x-acs-dingtalk-access-token": access_token
    }
    data = {
        "summary": summary,
        "start": {
            "dateTime": to_iso8601(start_datetime),
            "timeZone": BEIJING_TZ_NAME,
        },
        "end": {
            "dateTime": to_iso8601(end_datetime),
            "timeZone": BEIJING_TZ_NAME,
        }
    }
    calendar_response = await get_http_client().post(url, json=data, headers=headers)
    calendar_response.raise_for_status()
    return calendar_response.json() or {}


async def get_user_available_slots(
    interviewer_id: str,
    hour_interval: float = 1.0,
    max_days_to_search: int = 7,
    start_time: datetime = None,
    time_delta=1
):
    if start_time is None:
        start_time = now_beijing()
    result = await get_union_id_and_access_token_by_user_id(interviewer_id)
    # Step 1：获取面试官钉钉日历空闲时间
    if isinstance(result, str):
        # get_union_id_and_access_token_by_user_id 只会抛 DingTalkServiceError、
        # 不会返回 str；这里保留兜底但不能 ``raise result``（Python 不接受
        # 抛字符串，会变成 "exceptions must derive from BaseException" 的 TypeError）。
        raise DingTalkServiceError(str(result))
    union_id, access_token_info = result

    # 3. 从钉钉上获取面试官的日程安排
    try:

        # 用 replace 而不是重新构造 datetime：构造会丢掉 tzinfo，让日历查询窗口和
        # 空闲计算窗口（aware 北京时间）对不上；同时把起点压到 00:00，
        # 否则会漏掉当天更早的忙碌日程，把上午误判成全天空。
        start_datetime = (start_time + timedelta(days=time_delta)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        events: list[dict[str, Any]] = await get_calendar_list(
            union_id= union_id,
            access_token=cast(str, access_token_info.get("access_token")),
            time_min=start_datetime,
            time_max=start_datetime + timedelta(days=max_days_to_search),
        )
        busy_slots = []
        for event in events:
            busy_start = parse_event_boundary(event, "start")
            busy_end = parse_event_boundary(event, "end")
            # 缺字段的日程直接跳过，不能让一条脏数据毁掉整轮可用时间计算
            if busy_start is None or busy_end is None:
                continue
            busy_slots.append((busy_start, busy_end))
        available_slots: list[tuple[datetime, datetime]] = find_available_slot(
            busy_slots,
            hour_interval=hour_interval,
            start_date=start_datetime,
            max_days_to_search=max_days_to_search,
        )
    except Exception as e:
        logger.error("获取面试官可用时间失败: %s", e)
        raise DingTalkServiceError(
            f"获取面试官可用时间失败: {e}"
        ) from e
    return available_slots