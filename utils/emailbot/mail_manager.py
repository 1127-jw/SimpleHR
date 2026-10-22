"""邮件机器人全局单例管理器。"""
import asyncio
from typing import Optional

from fastapi import Request

from loguru import logger

from utils.emailbot.bot import MailBot
from utils.emailbot.config import MailSettings
from utils.emailbot.models import ParsedEmail
from utils.emailbot.rules import DEFAULT_RULES


async def notify(email: ParsedEmail, rule) -> None:
    # ---- 基本信息 ----
    print(f"UID:        {email.uid}")
    print(f"Message-ID: {email.message_id}")
    print(f"主题:       {email.subject}")
    print(f"日期:       {email.date}")

    # ---- 发件人 ----
    print(f"发件人姓名: {email.from_.name}")
    print(f"发件人地址: {email.from_.address}")
    print(f"发件地址(小写): {email.sender}")

    # ---- 收件人 ----
    for addr in email.to:
        print(f"收件人: {addr.name} <{addr.address}>")

    for addr in email.cc:
        print(f"抄送:   {addr.name} <{addr.address}>")

    # ---- 正文 ----
    print(f"纯文本: {email.text[:200]}")   # 前 200 字
    print(f"HTML:   {email.html[:200]}")   # 原始 HTML

    # ---- 附件 ----
    for att in email.attachments:
        print(f"附件名:   {att.filename}")
        print(f"类型:     {att.content_type}")
        print(f"大小:     {att.size} bytes")
        print(f"落盘路径: {att.saved_path}")

    # ---- 原始 MIME 头（任意头都可取）----
    raw = email.raw
    print(f"Reply-To:     {raw.get('Reply-To')}")
    print(f"References:   {raw.get('References')}")
    print(f"Content-Type: {raw.get_content_type()}")


class MailManager:
    """邮件机器人全局单例。"""

    _instance: Optional["MailManager"] = None
    _bot: Optional[MailBot] = None
    _poll_task: Optional[asyncio.Task] = None
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
        smtp_host: str = "smtp.163.com",
        smtp_port: int = 465,
        smtp_starttls: bool = False,
        smtp_implicit_ssl: bool = True,
        email: str | None = None,
        password: str | None = None,
        poll_interval: int = 20,
        attach_dir: str = "./attachments",
        state_file: str = "./mailbot_state.json",
        rules: list | None = None,
        on_match=notify,
    ) -> "MailManager":
        """初始化邮件机器人（全局只初始化一次）。"""
        mgr = cls.get()

        if mgr._initialized:
            logger.warning("MailManager 已初始化，跳过重复 init")
            return mgr

        logger.info("MailManager 初始化中...")

        settings = MailSettings(
            imap_host=imap_host,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_starttls=smtp_starttls,
            smtp_implicit_ssl=smtp_implicit_ssl,
            email=email,
            password=password,
            poll_interval=poll_interval,
            attach_dir=attach_dir,
            state_file=state_file,
        )

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

        mgr._poll_task = asyncio.create_task(mgr._poll_loop())
        mgr._initialized = True

        logger.info("MailManager 初始化完成，轮询每 {}s 一次", poll_interval)
        return mgr

    @classmethod
    async def _poll_loop(cls) -> None:
        """后台轮询循环。"""
        mgr = cls.get()
        while True:
            try:
                await mgr._bot.poll_once()
            except Exception:
                logger.exception("poll round crashed, will retry")
            await asyncio.sleep(mgr._bot.settings.poll_interval)

    # ========== 关闭 ==========
    @classmethod
    async def close(cls) -> None:
        """关闭邮件机器人并清理资源。"""
        mgr = cls.get()

        if not mgr._initialized:
            logger.warning("MailManager 未初始化，跳过 close")
            return

        logger.info("MailManager 关闭中...")

        # 1. 取消轮询
        if mgr._poll_task and not mgr._poll_task.done():
            mgr._poll_task.cancel()
            try:
                await mgr._poll_task
            except asyncio.CancelledError:
                pass

        # 2. 断开连接
        if mgr._bot and mgr._bot.is_connected:
            await mgr._bot.close()

        mgr._bot = None
        mgr._poll_task = None
        mgr._initialized = False

        logger.info("MailManager 已关闭")

def get_mail_bot(request: Request) -> MailBot:
    """从全局单例获取 bot 实例。"""
    return MailManager.get().bot

