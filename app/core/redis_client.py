import redis.asyncio as redis
from redis.asyncio import ConnectionPool
from app.core.config import get_settings

settings = get_settings()

REDIS_URL = settings.REDIS_URL
pool: ConnectionPool | None = None
redis_client: redis.Redis | None = None


async def init_redis():
    global pool, redis_client

    pool = ConnectionPool.from_url(
        REDIS_URL,
        max_connections=20,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=10,
        retry_on_timeout=True,
    )
    redis_client = redis.Redis(connection_pool=pool)


async def close_redis():
    global pool, redis_client

    if redis_client:
        await redis_client.aclose()
    if pool:
        await pool.disconnect()


def get_redis() -> redis.Redis:
    """返回全局 Redis 客户端（单例）。

    刻意**不是** async：这里只是取一个模块级单例，没有任何 I/O。

    曾经写成 ``async def``，结果三处调用方漏了 await，拿到的 ``r`` 其实是
    coroutine，一调 ``.set()`` / ``.eval()`` 就报
    ``'coroutine' object has no attribute 'set'`` —— 超时巡检因此连续失败几十轮。
    与 ``get_http_client()`` / ``get_checkpointer()`` 保持同一种写法。
    """
    if redis_client is None:
        raise RuntimeError("Redis 未初始化")
    return redis_client