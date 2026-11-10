import os
import sys
import asyncio

from utils.emailbot.mail_manager import MailManager

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    # 强制 uvicorn 在 Windows 上使用 SelectorEventLoop
    import uvicorn.loops.asyncio as _uvicorn_asyncio_loop
    _uvicorn_asyncio_loop.asyncio_loop_factory = lambda use_subprocess=False: asyncio.SelectorEventLoop

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.exceptions import AppException, BusinessException, ExternalServiceError
from app.core.request_context import new_request_id, set_request_id

from app.routers.user_router import router as user_router
from app.routers.position_router import router as position_router
from app.routers.candidate_router import router as candidate_router

from contextlib import asynccontextmanager
from app.core.redis_client import init_redis, close_redis
from app.core.http_client import close_http_client

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.core.logging_config import setup_logging, stop_logging
    setup_logging(level="INFO")
    await init_redis()
    await MailManager.init(
        imap_host=os.getenv("IMAP_HOST", "imap.163.com"),
        smtp_host=os.getenv("SMTP_HOST", "smtp.163.com"),
        smtp_port=int(os.getenv("SMTP_PORT", "465")),
        email=os.getenv("MAIL_USERNAME"),
        password=os.getenv("MAIL_PASSWORD"),
    )

    yield

    await MailManager.close()
    await close_redis()
    await close_http_client()
    stop_logging()


def create_app() -> FastAPI:

    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        debug=settings.debug,
        lifespan=lifespan,

    )
    return app
app = create_app()

# ==================== 全局异常处理器 ====================

@app.exception_handler(BusinessException)
async def business_exception_handler(request: Request, exc: BusinessException):
    """业务异常 → HTTP 400。"""
    return JSONResponse(
        status_code=400,
        content={"detail": exc.message, "code": exc.code},
    )

@app.exception_handler(ExternalServiceError)
async def external_service_exception_handler(request: Request, exc: ExternalServiceError):
    """第三方服务异常 → HTTP 502。"""
    return JSONResponse(
        status_code=502,
        content={"detail": exc.message, "code": exc.code},
    )

@app.exception_handler(AppException)
async def app_exception_handler(request: Request, exc: AppException):
    """兜底应用异常 → HTTP 500。"""
    return JSONResponse(
        status_code=500,
        content={"detail": exc.message, "code": exc.code},
    )

#跨域
"""
allow_origins=["*"]：允许所有来源的跨域请求。* 是通配符，表示不限制请求的来源域名。
allow_credentials=False：不允许携带凭证（如 Cookie、HTTP 认证信息等）。
allow_methods=["*"]：允许所有 HTTP 方法（GET、POST、PUT、DELETE 等）。
allow_headers=["*"]：允许所有请求头。
"""
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # 与 allow_origins=["*"] 不兼容
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(user_router)
app.include_router(position_router)
app.include_router(candidate_router)


# ==================== 请求 ID ====================
# 放在最外层（后注册的 middleware 更靠外），保证后续所有 handler / 日志
# 都能读到 request_id。
@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """为每个请求分配 / 透传 request id，并写回响应头。"""
    request_id = request.headers.get("X-Request-ID") or new_request_id()
    set_request_id(request_id)
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.get("/")
async def root():
    return {"message": "Hello World"}