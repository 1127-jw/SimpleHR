"""本轮加固改动的回归测试。

覆盖：
1. refresh token 原子消费（并发下只能被兑换一次）
2. ``UserSH`` 不暴露密码字段
3. 检查点存储未初始化时的 fail-fast
4. 邮件回复超时巡检在图模式关闭时空跑
"""
import asyncio
import datetime as dt

import pytest

from app.core import auth
from app.schemas.user_schema import UserSH


# ---------------- 1. refresh token 原子消费 ----------------

async def test_refresh_token_consumed_atomically(fake_redis):
    """并发刷新同一 token：只有一个请求能换到新令牌。

    旧实现用 ``exists()`` + ``delete()`` 两次往返，两个并发请求会同时
    通过检查，导致同一 refresh token 被兑换两次。
    """
    tokens = await auth.create_tokens("a@b.com", fake_redis)

    results = await asyncio.gather(
        auth.refresh_access_token(tokens["refresh_token"], fake_redis),
        auth.refresh_access_token(tokens["refresh_token"], fake_redis),
        return_exceptions=True,
    )

    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, Exception)]

    assert len(successes) == 1, "同一 refresh token 只能被兑换一次"
    assert len(failures) == 1
    assert "revoked" in str(failures[0])


async def test_consume_refresh_token_returns_none_when_missing(fake_redis):
    assert await auth._consume_refresh_token(fake_redis, "never-issued") is None


async def test_refresh_still_issues_new_token_pair(fake_redis):
    """正常路径：刷新成功后应拿到新的 access + refresh。"""
    tokens = await auth.create_tokens("a@b.com", fake_redis)

    new_tokens = await auth.refresh_access_token(tokens["refresh_token"], fake_redis)

    assert new_tokens["access_token"] != tokens["access_token"]
    assert new_tokens["refresh_token"] != tokens["refresh_token"]
    # 旧 jti 已删除
    old = auth._decode(tokens["refresh_token"], auth.REFRESH_SECRET)
    assert await fake_redis.exists(f"refresh:{old['jti']}") == 0


# ---------------- 2. 用户响应不暴露密码 ----------------

def test_user_sh_has_no_password_field():
    """``/user/me`` 直接由 ``UserSH`` 序列化，绝不能带出密码哈希。"""
    fields = set(UserSH.model_fields)
    assert "password" not in fields
    assert "_password" not in fields


# ---------------- 3. 检查点存储 fail-fast ----------------

async def test_get_checkpointer_before_init_raises():
    """未在 lifespan 初始化就取用检查点，必须立刻报错而不是静默建连。"""
    from app.core import checkpointer

    # 测试进程不会调用 init_checkpointer()
    if checkpointer._saver is None:
        with pytest.raises(RuntimeError, match="未初始化"):
            checkpointer.get_checkpointer()


# ---------------- 4. 超时巡检的空跑保护 ----------------

async def test_sweep_noop_when_graph_mode_disabled(monkeypatch):
    """GRAPH_OPEN=False 时巡检直接返回，不触碰 Redis / 数据库。"""
    from app.core import config
    from app.tasks import timeout_tasks

    monkeypatch.setattr(timeout_tasks.settings, "GRAPH_OPEN", False)

    # Redis 未初始化也不会抛错（函数应在更早处返回）
    assert await timeout_tasks.sweep_stale_email_replies() == 0
