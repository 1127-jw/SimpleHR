from fastapi import HTTPException

from alibabacloud_dingtalk.oauth2_1_0.client import Client as dingtalkoauth2_1_0Client
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_dingtalk.oauth2_1_0 import models as dingtalkoauth_2__1__0_models

from datetime import datetime, timezone, timedelta
from urllib.parse import quote, urljoin
from redis.asyncio import Redis
import httpx
from loguru import logger

from app.repository.user_repo import UserRepo
from app.core.config import get_settings
from utils.time_ISO8601_datatime import datetime_to_iso8601_beijing

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
    async with httpx.AsyncClient() as client:
        resp = await client.post(
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
    ac_cache_key = f"dingtalk:user_actoken:{state}"
    re_cache_key = f"dingtalk:user_retoken:{state}"
    ac_token = await r.get(ac_cache_key)
    re_token = await r.get(re_cache_key)
    if not ac_token:
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

async def get_dingtalk_user(session,state,access_token: str, union_id: str="me"):
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{DINGTALK_BASE}/v1.0/contact/users/{union_id}",
            headers={
                "x-acs-dingtalk-access-token": access_token,
            },
        )
        resp.raise_for_status()
        resp = resp.json()
        nick = resp["nick"]
        mobile = resp["mobile"]
        open_id = resp["openId"]
        union_id = resp["unionId"]

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
    time_min_iso8601: str = datetime_to_iso8601_beijing(time_min)
    time_max_iso8601: str = datetime_to_iso8601_beijing(time_max)

    # 对时间字符串进行 URL 编码，防止 '+' 被识别为空格
    time_min_encoded = quote(time_min_iso8601)
    time_max_encoded = quote(time_max_iso8601)

    path = f"/v1.0/calendar/users/{union_id}/calendars/primary/events?timeMin={time_min_encoded}&timeMax={time_max_encoded}"
    return urljoin(DINGTALK_BASE, path)

#获取日程列表
async def get_calendar_list(union_id: str, access_token: str, time_min: datetime, time_max: datetime):
    url = build_get_calendar_list_url(union_id, time_min, time_max)
    headers = {
        "x-acs-dingtalk-access-token": access_token
    }
    async with httpx.AsyncClient() as client:
        calendar_resp = await client.get(url,headers=headers)
        calendar_resp.raise_for_status()
        calendar_data = calendar_resp.json() or {}
        events = calendar_data.get("events")
        return events


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
            "dateTime": datetime_to_iso8601_beijing(start_datetime),
            "timeZone": "Asia/Shanghai",
        },
        "end": {
            "dateTime": datetime_to_iso8601_beijing(end_datetime),
            "timeZone": "Asia/Shanghai",
        }
    }
    async with httpx.AsyncClient() as client:
        calendar_response = await client.post(url, json=data, headers=headers)
        calendar_response.raise_for_status()
        calendar_data = calendar_response.json() or {}
        return calendar_data