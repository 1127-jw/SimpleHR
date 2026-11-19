"""基于 Redis 的滑动窗口限流。

为什么用滑动窗口而不是计数器：固定窗口在窗口边界会出现 2 倍突发。
这里用 ZSET 记录每次请求的时间戳，按 ``now - window`` 清理过期成员，
天然平滑。

整个判断 + 写入用一段 Lua 脚本完成，保证原子性（多 worker 并发安全）。
"""
from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from redis.asyncio import Redis

# KEYS[1] 限流键；ARGV: now(秒) / window(秒) / limit / member
_SLIDING_WINDOW_LUA = """
local key    = KEYS[1]
local now    = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit  = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCARD', key)
if count < limit then
    redis.call('ZADD', key, now, member)
    redis.call('PEXPIRE', key, math.floor(window * 1000))
    return 1
end
return 0
"""


async def hit(
    r: Redis,
    key: str,
    *,
    limit: int,
    window: int,
) -> bool:
    """记录一次请求。

    ``limit <= 0`` 表示**关闭限流**（放行且不计数）。

    这与 ``app/core/limiters.py`` 里 ``LLM_RPM_LIMIT <= 0`` 的语义保持一致。
    此前本函数对 ``limit <= 0`` 的处理是「永远不满足 count < limit」，
    即静默变成全站 429 —— 把「关掉限流」的配置变成了「关掉服务」。

    Returns:
        True 表示放行，False 表示已超限。
    """
    import time

    if limit <= 0:
        return True

    allowed = await r.eval(
        _SLIDING_WINDOW_LUA,
        1,
        key,
        time.time(),
        window,
        limit,
        f"{time.time()}-{uuid.uuid4().hex}",
    )
    return bool(allowed)


async def enforce(
    r: Redis,
    key: str,
    *,
    limit: int,
    window: int,
    detail: str = "请求过于频繁，请稍后再试",
) -> None:
    """超限则直接抛 429。"""
    if not await hit(r, key, limit=limit, window=window):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=detail,
        )
