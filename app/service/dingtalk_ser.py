from fastapi import HTTPException

from alibabacloud_dingtalk.oauth2_1_0.client import Client as dingtalkoauth2_1_0Client
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_dingtalk.oauth2_1_0 import models as dingtalkoauth_2__1__0_models

from redis.asyncio import Redis
from app.core.config import get_settings
from app.models.user import UserModel

settings = get_settings()

def get_dingtalk_oauth_client() -> dingtalkoauth2_1_0Client:
    """
    使用 Token 初始化账号Client
    @return: Client
    @throws Exception
    """
    config = open_api_models.Config()
    config.protocol = 'https'
    config.region_id = 'central'
    return dingtalkoauth2_1_0Client(config)

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
    print("request:",request)

    resp = client.get_user_token(request)
    print("resp",resp)
    access_token = resp.body.access_token
    refreshToken = resp.body.refresh_token
    if not access_token:
        raise HTTPException(status_code=502, detail="钉钉返回的 access_token 为空")
    if not refreshToken:
        raise HTTPException(status_code=502, detail="钉钉返回的 refreshToken 为空")

    ac_cache_key = f"dingtalk:user_actoken:{state}"
    re_cache_key = f"dingtalk:user_retoken:{state}"

    await r.set(ac_cache_key, access_token, ex=7200)
    await r.set(re_cache_key, refreshToken, ex=30*24*60*60)

    return {
        "access_token": access_token,
        "state": state,
    }