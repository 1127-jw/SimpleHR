"""请求 ID 中间件：为每个请求分配 / 透传 request id。"""
from fastapi import Request

from app.core.request_context import new_request_id, set_request_id


async def request_id_middleware(request: Request, call_next):
    """为每个请求分配 / 透传 request id，并写回响应头。

    放在最外层（后注册的 middleware 更靠外），保证后续所有 handler / 日志都能读到 request_id。
    """
    request_id = request.headers.get("X-Request-ID") or new_request_id()
    set_request_id(request_id)
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response
