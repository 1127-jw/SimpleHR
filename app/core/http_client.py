"""全局共享的 ``httpx.AsyncClient``。
这里复用一个 client，生命周期由 FastAPI lifespan 管理：

    # app/main.py
    await close_http_client()   # 关闭时

注意：**不要**在请求处理里 close 它，也不要跨事件循环复用。
"""
from __future__ import annotations

import httpx

# 连接池参数：并发上限远大于单 worker 的数据库连接数，够用即可
_DEFAULT_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20)
_DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """返回全局复用的 client（懒创建）。"""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT, limits=_DEFAULT_LIMITS)
    return _client


async def close_http_client() -> None:
    """关闭全局 client（应用退出时调用）。"""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None
