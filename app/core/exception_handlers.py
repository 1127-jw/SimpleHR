"""全局异常处理器配置。"""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.exceptions import AppException, BusinessException, ExternalServiceError


def setup_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器。"""

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
