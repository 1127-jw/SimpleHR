from redis.asyncio import Redis
import jwt
import uuid

from datetime import datetime, timedelta, timezone
from pydantic import BaseModel, Field

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from sqlalchemy.ext.asyncio import AsyncSession
from typing import Annotated, Optional, Literal

from app.core.config import get_settings
from app.core.db import get_db
from app.repository.user_repo import UserRepo
from app.models.user import UserModel, UserStatus

settings = get_settings()


# 建议从环境变量读取
ACCESS_SECRET = settings.ACCESS_SECRET
REFRESH_SECRET = settings.REFRESH_SECRET
ALGORITHM = "HS256"

ACCESS_EXPIRE_MINUTES = settings.ACCESS_EXPIRE_MINUTES
REFRESH_EXPIRE_DAYS = settings.REFRESH_EXPIRE_DAYS


# Token 载荷模型
class TokenPayload(BaseModel):
    sub: str
    jti: str = Field(default_factory=lambda: uuid.uuid4().hex)
    # type: str  # "access" 或 "refresh"
    type: Literal["access","refresh"]  # "access" 或 "refresh"
    exp: datetime
    # v: int #版本号

#签发与验证
def _encode(payload: dict, secret: str) -> str:
    return jwt.encode(payload, secret, algorithm=ALGORITHM)

def _decode(token: str, secret: str) -> dict:
    return jwt.decode(token, secret, algorithms=[ALGORITHM])

# 原子消费 refresh token：GET + DEL 必须在同一条 Lua 脚本中完成。
# 若拆成 exists() + delete() 两次往返，并发刷新会同时通过存在性检查，
# 导致同一 token 被兑换两次（refresh token 轮换失去意义）。
_CONSUME_REFRESH_LUA = """
local value = redis.call('GET', KEYS[1])
if value then
    redis.call('DEL', KEYS[1])
    return value
end
return nil
"""


async def _consume_refresh_token(r: Redis, jti: str) -> str | None:
    """原子地取出并删除 ``refresh:{jti}``；不存在时返回 None。"""
    return await r.eval(_CONSUME_REFRESH_LUA, 1, f"refresh:{jti}")


async def create_tokens(user_email: str, r: Redis) -> dict:
    now = datetime.now(timezone.utc)
    access_exp = now + timedelta(minutes=ACCESS_EXPIRE_MINUTES)
    refresh_exp = now + timedelta(days=REFRESH_EXPIRE_DAYS)
    refresh_jti = uuid.uuid4().hex

    access_token = _encode({
        "sub": user_email,
        "jti": uuid.uuid4().hex,
        "type": "access",
        "exp": access_exp,
        "iat": now.timestamp(),
    }, ACCESS_SECRET)

    refresh_token = _encode({
        "sub": user_email,
        "jti": refresh_jti,
        "type": "refresh",
        "exp": refresh_exp,
        # iat 是「吊销此前所有会话」的判据：改密时写入 pwd_reset_at，
        # 签发时间早于它的一律失效。
        # 这里刻意用带小数的 timestamp 而不是 pyjwt 默认的整秒 iat：
        # 整秒精度下「同一秒内先改密、后登录」的新 token 会等于 reset_at，
        # 比较严格小于虽然能放行，但「同一秒内先发 token、后改密」的旧 token
        # 也会等于 reset_at 而被放过——两边不可能同时正确，用浮点即可都正确。
        "iat": now.timestamp(),
    }, REFRESH_SECRET)

    await r.setex(
        f"refresh:{refresh_jti}",
        int(REFRESH_EXPIRE_DAYS * 86400),
        user_email,
    )

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
    }


async def revoke_user_sessions(user_email: str, r: Redis) -> None:
    """让该用户**已签发**的所有 refresh token 立即失效。

    改密 / 强制下线时调用。实现方式是记一个「会话纪元」：
    之后签发的 token 带 ``iat``，早于该时间点的一律拒绝兑换。

    为什么不用「枚举该用户的 jti 再逐个删」：那需要额外的用户→jti 索引与
    多键删除，在「用户密码刚泄露、需要立刻止血」的场景下又多一层状态；
    时间戳判据只有一个键，且天然覆盖历史 token。
    """
    await r.set(
        f"pwd_reset_at:{user_email}",
        datetime.now(timezone.utc).timestamp(),
        ex=int(REFRESH_EXPIRE_DAYS * 86400),
    )


async def _reject_if_session_revoked(user_email: str, payload: dict, r: Redis) -> None:
    """签发时间早于最近一次改密时间 → 视为已吊销。"""
    reset_at = await r.get(f"pwd_reset_at:{user_email}")
    if reset_at is None:
        return
    issued_at = payload.get("iat")
    if issued_at is None:
        # 老 token 没有 iat：保守拒绝，反正过期后会重新登录
        raise ValueError("Refresh token has been revoked")
    if float(issued_at) < float(reset_at):
        raise ValueError("Refresh token has been revoked")


async def refresh_access_token(refresh_token: str, r: Redis) -> dict:
    try:
        payload = _decode(refresh_token, REFRESH_SECRET)
    except jwt.ExpiredSignatureError:
        raise ValueError("Refresh token expired")
    except jwt.InvalidTokenError:
        raise ValueError("Invalid refresh token")

    if payload.get("type") != "refresh":
        raise ValueError("Invalid token type")

    jti = payload["jti"]
    user_id = payload["sub"]

    await _reject_if_session_revoked(user_id, payload, r)

    # 原子消费：只有抢到该 jti 的那一次请求能继续签发新令牌
    if await _consume_refresh_token(r, jti) is None:
        raise ValueError("Refresh token has been revoked")

    return await create_tokens(user_id, r)


async def revoke_refresh_token(refresh_token: str, r: Redis) -> None:
    try:
        payload = _decode(refresh_token, REFRESH_SECRET)
    except jwt.InvalidTokenError:
        return

    jti = payload.get("jti")
    if jti:
        await r.delete(f"refresh:{jti}")





oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/user/token")

async def get_current_user(
        session:Annotated[AsyncSession, Depends(get_db)],
        token: str = Depends(oauth2_scheme)
                           ):

    try:
        payload = _decode(token, ACCESS_SECRET)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    if payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")
    async with session.begin():
        #1 get user
        user_repo = UserRepo(session)
        user:Optional[UserModel] = await user_repo.get_by_email(str(payload["sub"]))
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,detail="User not found")
        if user.status != UserStatus.ACTIVE:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,detail="User not active")
    return user

async def get_super_user(
        current_user: UserModel = Depends(get_current_user),
):
    if current_user.is_superuser:
        return current_user
    else:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,detail="权限不足")


async def get_hr_or_superuser(
        current_user: UserModel = Depends(get_current_user),
):
    """HR 或超级管理员权限。"""
    if current_user.is_hr or current_user.is_superuser:
        return current_user
    else:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="权限不足")