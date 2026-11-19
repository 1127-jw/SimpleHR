"""路由统一注册入口。"""
from fastapi import FastAPI

from app.routers.user_router import router as user_router
from app.routers.position_router import router as position_router
from app.routers.candidate_router import router as candidate_router
from app.routers.mail_submission_router import router as mail_submission_router
from app.routers.calendar_router import router as calendar_router


def setup_routers(app: FastAPI) -> None:
    """注册所有路由。"""
    app.include_router(user_router)
    app.include_router(position_router)
    app.include_router(candidate_router)
    app.include_router(mail_submission_router)
    app.include_router(calendar_router)
