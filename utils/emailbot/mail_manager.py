"""邮件机器人全局单例管理器。"""
import asyncio
from typing import Optional

from fastapi import Request
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from loguru import logger

from utils.emailbot.bot import MailBot
from utils.emailbot.config import MailSettings

from utils.emailbot.rules import DEFAULT_RULES

from utils.emailbot.notify import notify


class MailManager:
    """邮件机器人全局单例。"""

    _instance: Optional["MailManager"] = None
    _scheduler: Optional[AsyncIOScheduler] = None
    _bot: Optional[MailBot] = None
    _initialized: bool = False

    def __new__(cls) -> "MailManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def get(cls) -> "MailManager":
        """获取全局单例。"""
        return cls()

    @property
    def bot(self) -> MailBot:
        if self._bot is None:
            raise RuntimeError("MailManager 未初始化，请先调用 init()")
        return self._bot

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    # ========== 初始化 ==========
    @classmethod
    async def init(
        cls,
        *,
        imap_host: str = "imap.163.com",
        imap_port: int | None = None,
        imap_ssl: bool | None = None,
        imap_mailbox: str | None = None,
        smtp_host: str = "smtp.163.com",
        smtp_port: int = 465,
        smtp_starttls: bool = False,
        smtp_implicit_ssl: bool = True,
        email: str | None = None,
        password: str | None = None,
        poll_interval: int = 20,
        fetch_limit: int | None = None,
        attach_dir: str = "./attachments",
        state_file: str = "./mailbot_state.json",
        auto_reply: bool | None = None,
        reply_text: str | None = None,
        rules: list | None = None,
        on_match=notify,
    ) -> "MailManager":
        """初始化邮件机器人（全局只初始化一次）。"""
        mgr = cls.get()

        if mgr._initialized:
            logger.warning("MailManager 已初始化，跳过重复 init")
            return mgr

        logger.info("MailManager 初始化中...")

        # 只传非 None 参数，让 MailSettings 使用其默认值
        settings_kwargs = {
            'imap_host': imap_host,
            'smtp_host': smtp_host,
            'smtp_port': smtp_port,
            'smtp_starttls': smtp_starttls,
            'smtp_implicit_ssl': smtp_implicit_ssl,
            'email': email,
            'password': password,
            'poll_interval': poll_interval,
            'attach_dir': attach_dir,
            'state_file': state_file,
            'imap_port': imap_port,
            'imap_ssl': imap_ssl,
            'imap_mailbox': imap_mailbox,
            'fetch_limit': fetch_limit,
            'auto_reply': auto_reply,
            'reply_text': reply_text,
        }
        settings_kwargs = {k: v for k, v in settings_kwargs.items() if v is not None}
        settings = MailSettings(**settings_kwargs)

        mgr._bot = MailBot(
            settings,
            rules=rules or DEFAULT_RULES,
            on_match=on_match,
        )

        try:
            await mgr._bot.connect()
            logger.info("邮箱连接成功: {}", settings.email)
        except Exception:
            logger.exception("邮箱连接失败，继续启动（后台会重试）")

        # 使用 APScheduler 定时轮询，
        # max_instances = 1：上次轮询未完成时不会触发新轮询,防止重叠执行
        # coalesce = True：服务暂停期间错过的轮询只补执行一次，不会积压
        # shutdown(wait=False)：关闭时立即停止，不用等当前轮询结束
        cls._scheduler = AsyncIOScheduler()
        cls._scheduler.add_job(
            cls._poll_once,     # 要执行的函数
            'interval',  # 触发器类型
            seconds=poll_interval,# 间隔时间（秒）
            id='mail_poll',     # 任务唯一标识
            max_instances=1,    # 最大并发实例数
            coalesce=True,      # 合并错过的执行
        )
        cls._scheduler.start()
        mgr._initialized = True

        logger.info("MailManager 初始化完成，轮询每 {}s 一次", poll_interval)
        return mgr

    @classmethod
    async def _poll_once(cls) -> None:
        """单次轮询任务。"""
        mgr = cls.get()
        try:
            await mgr._bot.poll_once()
        except Exception:
            logger.exception("poll round crashed, will retry")

    # ========== 关闭 ==========
    @classmethod
    async def close(cls) -> None:
        """关闭邮件机器人并清理资源。"""
        mgr = cls.get()

        if not mgr._initialized:
            logger.warning("MailManager 未初始化，跳过 close")
            return

        logger.info("MailManager 关闭中...")

        # 1. 停止调度器
        if cls._scheduler:
            cls._scheduler.shutdown(wait=False)
            cls._scheduler = None

        # 2. 断开连接
        if mgr._bot and mgr._bot.is_connected:
            await mgr._bot.close()

        mgr._bot = None
        mgr._initialized = False

        logger.info("MailManager 已关闭")

def get_mail_bot(request: Request) -> MailBot:
    """从全局单例获取 bot 实例。"""
    return MailManager.get().bot

def get_mail_bot_() -> MailBot:
    """从全局单例获取 bot 实例。"""
    return MailManager.get().bot