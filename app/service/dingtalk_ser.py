from fastapi import HTTPException

from alibabacloud_dingtalk.oauth2_1_0.client import Client as dingtalkoauth2_1_0Client
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_dingtalk.oauth2_1_0 import models as dingtalkoauth_2__1__0_models


from redis.asyncio import Redis
from app.repository.user_repo import UserRepo
from app.core.config import get_settings

settings = get_settings()

import httpx

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

# class DingtalkService():
#
#     # dingtalk_api_base_url = "https://oapi.dingtalk.com"
#     # user_accesstoken_url = "/v1.0/oauth2/userAccessToken"
#     #
#     # dingtalk_accesstoken = urljoin(dingtalk_api_base_url, user_accesstoken_url)
#     pass


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



