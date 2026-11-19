"""邮件机器人全局单例管理器。"""
import logging
from typing import Optional

from fastapi import Request

from utils.emailbot.bot import MailBot
from utils.emailbot.config import MailSettings

from utils.emailbot.rules import DEFAULT_RULES

from utils.emailbot.notify import notify

from app.core.config import get_settings
from app.tasks.mail_poll_loop import MailPollTimer

logger = logging.getLogger(__name__)


class MailManager:
    """邮件机器人全局单例。"""

    _instance: Optional["MailManager"] = None
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
        poll_interval: int = 20,   # 仅透传给 MailSettings（独立部署的 bot 用）；
                                   # 应用内轮询节奏由 MailPollTimer + 调度配置决定
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

        # 开关内聚在这里：MAIL_BOT_ENABLED=false（CI / 测试环境）时直接跳过，
        # 调用方无需自己判断，保证 init / close 调用始终对称
        if not get_settings().MAIL_BOT_ENABLED:
            logger.info("MAIL_BOT_ENABLED=false，跳过邮件机器人初始化")
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
            logger.info("邮箱连接成功: %s", settings.email)
        except Exception:
            logger.exception("邮箱连接失败，继续启动（后台会重试）")

        # 轮询计时器：节奏由 Settings.MAIL_POLL_INTERVAL_SECONDS 决定（默认 15s），
        # 是独立开关，与 DEBUG / 全局调度模式无关
        await MailPollTimer.start(cls._poll_once)
        mgr._initialized = True

        logger.info("MailManager 初始化完成，调度模式=%s", MailPollTimer._resolve_mode())
        return mgr

    @classmethod
    async def _poll_once(cls) -> None:
        """单次轮询任务。

        异常**不就地吞掉**：``MailPollTimer`` 已并入 ``PeriodicTaskManager``，
        由基类的 ``run_with_retry`` 负责重试，重试耗尽后计数并告警。
        在这里吞掉会让连续失败永远反映不出来。
        """
        mgr = cls.get()
        if mgr._bot is None:
            raise RuntimeError("MailBot 未初始化")
        await mgr._bot.poll_once()

    # ========== 关闭 ==========
    @classmethod
    async def close(cls) -> None:
        """关闭邮件机器人并清理资源。"""
        mgr = cls.get()

        if not mgr._initialized:
            logger.info("MailManager 未初始化（可能已被 MAIL_BOT_ENABLED=false 跳过），无需关闭")
            return

        logger.info("MailManager 关闭中...")

        # 1. 停止轮询计时器（先停表，避免关闭过程中又触发一轮）
        await MailPollTimer.close()

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