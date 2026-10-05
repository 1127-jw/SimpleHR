import sys
import asyncio

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    # 强制 uvicorn 在 Windows 上使用 SelectorEventLoop
    import uvicorn.loops.asyncio as _uvicorn_asyncio_loop
    _uvicorn_asyncio_loop.asyncio_loop_factory = lambda use_subprocess=False: asyncio.SelectorEventLoop

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.routers.user_router import router as user_router
from contextlib import asynccontextmanager
from app.core.redis_client import init_redis, close_redis



@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_redis()

    yield

    await close_redis()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        debug=settings.debug,
        lifespan=lifespan,

    )


    return app


app = create_app()

#跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(user_router)