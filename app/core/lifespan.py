import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.core.config import get_settings
from app.core.redis_client import init_redis, close_redis
from app.core.http_client import close_http_client
from app.core.logging_config import setup_logging, stop_logging

logger = logging.getLogger(__name__)
settings = get_settings()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理。"""
    from app.core.checkpointer import init_checkpointer, close_checkpointer
    from app.tasks.timeout_tasks import TimeoutSweepManager
    from app.tasks.mail_submission_tasks import MailSubmissionSweepManager
    from utils.emailbot.mail_manager import MailManager

    setup_logging(level="INFO")
    await init_redis()
    await init_checkpointer()
    await MailManager.init(
        imap_host=os.getenv("IMAP_HOST", "imap.163.com"),
        smtp_host=os.getenv("SMTP_HOST", "smtp.163.com"),
        smtp_port=int(os.getenv("SMTP_PORT", "465")),
        email=os.getenv("MAIL_USERNAME"),
        password=os.getenv("MAIL_PASSWORD"),
    )
    await TimeoutSweepManager.init()
    await MailSubmissionSweepManager.init()

    yield

    await MailSubmissionSweepManager.close()
    await TimeoutSweepManager.close()
    await MailManager.close()
    await close_redis()
    await close_http_client()
    await close_checkpointer()
    stop_logging()