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


async def get_redis() -> redis.Redis:
    if redis_client is None:
        raise RuntimeError("Redis 未初始化")
    return redis_client