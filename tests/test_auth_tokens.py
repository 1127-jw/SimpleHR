"""Token 签发 / 过期 / 吊销。

不连真实 Redis，用 conftest.FakeRedis 覆盖 setex / exists / delete。
"""
import datetime as dt

import pytest

from app.core import auth


def _forged_refresh_token(seconds_to_live: int, jti: str = "forged-jti") -> str:
    """直接构造一个 refresh token（用于模拟已过期 / 已吊销）。"""
    return auth._encode(
        {
            "sub": "a@b.com",
            "jti": jti,
            "type": "refresh",
            "exp": dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds_to_live),
        },
        auth.REFRESH_SECRET,
    )


# ---------------- 签发 ----------------

async def test_issue_tokens_has_correct_claims(fake_redis):
    tokens = await auth.create_tokens("a@b.com", fake_redis)

    access = auth._decode(tokens["access_token"], auth.ACCESS_SECRET)
    refresh = auth._decode(tokens["refresh_token"], auth.REFRESH_SECRET)

    assert access["sub"] == "a@b.com"
    assert access["type"] == "access"
    assert refresh["type"] == "refresh"
    assert tokens["token_type"] == "bearer"


async def test_issue_tokens_registers_refresh_jti(fake_redis):
    tokens = await auth.create_tokens("a@b.com", fake_redis)
    refresh = auth._decode(tokens["refresh_token"], auth.REFRESH_SECRET)

    assert await fake_redis.exists(f"refresh:{refresh['jti']}") == 1


def test_expiry_follows_settings():
    """过期时间必须来自 Settings，而不是硬编码常量。"""
    from app.core.config import get_settings

    settings = get_settings()
    assert auth.ACCESS_EXPIRE_MINUTES == settings.ACCESS_EXPIRE_MINUTES
    assert auth.REFRESH_EXPIRE_DAYS == settings.REFRESH_EXPIRE_DAYS


# ---------------- 刷新 / 轮换 ----------------

async def test_refresh_rotates_and_invalidates_old_token(fake_redis):
    tokens = await auth.create_tokens("a@b.com", fake_redis)
    old_refresh = tokens["refresh_token"]

    new_tokens = await auth.refresh_access_token(old_refresh, fake_redis)

    assert new_tokens["refresh_token"] != old_refresh
    # 旧 refresh token 已被消费，不能再用（防重放）
    with pytest.raises(ValueError, match="revoked"):
        await auth.refresh_access_token(old_refresh, fake_redis)


async def test_token_with_access_type_rejected_as_refresh(fake_redis):
    """access / refresh 用不同密钥，所以这里要用 REFRESH_SECRET 签一个
    ``type=access`` 的 token，才能走到类型校验那一层。"""
    forged = auth._encode(
        {
            "sub": "a@b.com",
            "jti": "forged",
            "type": "access",
            "exp": dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5),
        },
        auth.REFRESH_SECRET,
    )

    with pytest.raises(ValueError, match="Invalid token type"):
        await auth.refresh_access_token(forged, fake_redis)


async def test_access_token_from_another_secret_is_rejected(fake_redis):
    """用 ACCESS_SECRET 签的 token 拿去刷新，签名校验就过不了。"""
    tokens = await auth.create_tokens("a@b.com", fake_redis)

    with pytest.raises(ValueError, match="Invalid refresh token"):
        await auth.refresh_access_token(tokens["access_token"], fake_redis)


# ---------------- 过期 ----------------

async def test_expired_refresh_token_is_rejected(fake_redis):
    with pytest.raises(ValueError, match="expired"):
        await auth.refresh_access_token(_forged_refresh_token(-1), fake_redis)


async def test_tampered_token_is_rejected(fake_redis):
    tokens = await auth.create_tokens("a@b.com", fake_redis)

    with pytest.raises(ValueError, match="Invalid refresh token"):
        await auth.refresh_access_token(tokens["refresh_token"] + "x", fake_redis)


# ---------------- 吊销 ----------------

async def test_revoke_blocks_subsequent_refresh(fake_redis):
    tokens = await auth.create_tokens("a@b.com", fake_redis)

    await auth.revoke_refresh_token(tokens["refresh_token"], fake_redis)

    with pytest.raises(ValueError, match="revoked"):
        await auth.refresh_access_token(tokens["refresh_token"], fake_redis)


async def test_revoke_with_garbage_token_is_noop(fake_redis):
    """伪造/损坏的 token 吊销时应静默返回，不能抛异常。"""
    await auth.revoke_refresh_token("not-a-jwt", fake_redis)
