import os
from typing import Literal
from redis.asyncio import Redis
import jwt
import uuid

from datetime import datetime, timedelta, timezone
from pydantic import BaseModel, Field
from app.core.config import get_settings

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

settings = get_settings()


# 建议从环境变量读取
ACCESS_SECRET = settings.ACCESS_SECRET
REFRESH_SECRET = settings.REFRESH_SECRET
ALGORITHM = "HS256"

ACCESS_EXPIRE_MINUTES = 30
REFRESH_EXPIRE_DAYS = 7

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")
REDIS_DB = os.getenv("REDIS_DB", "0")



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
    }, ACCESS_SECRET)

    refresh_token = _encode({
        "sub": user_email,
        "jti": refresh_jti,
        "type": "refresh",
        "exp": refresh_exp,
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

    if not await r.exists(f"refresh:{jti}"):
        raise ValueError("Refresh token has been revoked")

    await r.delete(f"refresh:{jti}")

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

async def get_current_user(token: str = Depends(oauth2_scheme)):
    try:
        payload = _decode(token, ACCESS_SECRET)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    if payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")

    return {"user_id": payload["sub"]}