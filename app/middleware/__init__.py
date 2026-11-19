"""中间件统一注册入口。"""
from fastapi import FastAPI

from app.middleware.cache_control_middleware import setup_no_store
from app.middleware.cors_middleware import setup_cors
from app.middleware.request_id_middleware import request_id_middleware


def setup_middlewares(app: FastAPI) -> None:
    """按顺序注册所有中间件。

    注意：后注册的 middleware 更靠外（先执行），因此顺序很重要：
    - 请求 ID 中间件最后注册 → 最外层 → 保证所有 handler 都能读到 request_id
    - CORS 中间件先注册 → 内层 → 在请求 ID 之后执行
    - 禁用缓存中间件夹在中间，只改写响应头，与顺序无关
    """
    setup_cors(app)
    setup_no_store(app)
    app.middleware("http")(request_id_middleware)
