"""滑动窗口限流的真实语义测试。

这些用例存在的意义：``FakeRedis.eval`` 早先完全忽略脚本文本、只做一次 GETDEL，
于是限流脚本在测试里恒返回真值——「限流生效了吗」这类问题根本测不出来。
"""
import time as time_module

import pytest

from app.core import rate_limit
from app.core.rate_limit import _SLIDING_WINDOW_LUA


class _Clock:
    """可推进的假时钟，只替换 time.time。"""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(time_module, "time", c)
    return c


async def test_blocks_over_limit(fake_redis, clock):
    """60 秒窗口、上限 3：第 4 次必须被拒。"""
    key = "login:ip:1.2.3.4"

    allowed = [
        await rate_limit.hit(fake_redis, key, limit=3, window=60) for _ in range(4)
    ]
    assert allowed == [True, True, True, False]


async def test_window_slides_instead_of_resetting(fake_redis, clock):
    """滑动窗口：过期请求滑出窗口后腾出名额，而不是整窗清零。"""
    key = "login:email:a@b.com"
    for _ in range(3):
        assert await rate_limit.hit(fake_redis, key, limit=3, window=60)

    clock.now += 30          # 仍在窗口内
    assert not await rate_limit.hit(fake_redis, key, limit=3, window=60)

    clock.now += 31          # 最早 3 次（t=1000）已滑出 60 秒窗口
    assert await rate_limit.hit(fake_redis, key, limit=3, window=60)


async def test_limit_one_blocks_immediately_on_second_request(fake_redis, clock):
    """limit=1 时第二次请求就该拒绝——旧 FakeRedis 会两次都放行。"""
    key = "k"
    assert await rate_limit.hit(fake_redis, key, limit=1, window=60)
    assert not await rate_limit.hit(fake_redis, key, limit=1, window=60)


async def test_enforce_raises_429(fake_redis, clock):
    from fastapi import HTTPException

    key = "k"
    for _ in range(5):
        await rate_limit.hit(fake_redis, key, limit=5, window=60)
    with pytest.raises(HTTPException) as exc:
        await rate_limit.enforce(fake_redis, key, limit=5, window=60)
    assert exc.value.status_code == 429


async def test_state_counts_are_isolated(fake_redis, clock):
    """不同 key 各算各的名额。"""
    for _ in range(2):
        assert await rate_limit.hit(fake_redis, "ip:1", limit=2, window=60)
    assert not await rate_limit.hit(fake_redis, "ip:1", limit=2, window=60)
    assert await rate_limit.hit(fake_redis, "ip:2", limit=2, window=60)


async def test_fake_redis_rejects_unknown_script(fake_redis):
    """未知 Lua 必须显式失败，不能静默返回成功。"""
    with pytest.raises(AssertionError, match="未实现"):
        await fake_redis.eval("return 1", 0)


def test_sliding_window_script_is_the_one_under_test():
    """确认测试覆盖的确实是生产用的那段脚本。"""
    assert "ZREMRANGEBYSCORE" in _SLIDING_WINDOW_LUA
