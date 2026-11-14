"""FastAPI 应用入口。

职责：组装应用（中间件、异常处理器、路由、生命周期），不含具体业务逻辑。
"""
import logging
import sys
import asyncio

from fastapi import FastAPI
from app.core.config import get_settings
from app.middleware import setup_middlewares
from app.core.exception_handlers import setup_exception_handlers
from app.routers import setup_routers
from app.core.lifespan import lifespan

logger = logging.getLogger(__name__)
settings = get_settings()

# Windows 事件循环策略设置（开发环境）
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    import uvicorn.loops.asyncio as _uvicorn_asyncio_loop
    _uvicorn_asyncio_loop.asyncio_loop_factory = lambda use_subprocess=False: asyncio.SelectorEventLoop


def create_app() -> FastAPI:
    """创建 FastAPI 应用实例。"""
    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        debug=settings.debug,
        lifespan=lifespan,
    )

    # 注册中间件
    setup_middlewares(app)
    # 注册异常处理器
    setup_exception_handlers(app)
    # 注册路由
    setup_routers(app)

    @app.get("/")
    async def root():
        return {"message": "Hello World"}

    return app


app = create_app()
