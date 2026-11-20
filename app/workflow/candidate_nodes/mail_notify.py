"""工作流邮件发送：统一的 bot 获取 / 连接 / 异常包装。

节点里凡是需要「发信失败就报错、让流程进 error 节点」的场合都走这里，
不要再各写一份（invite / confirm / 复核失败重发共用）。
"""
from __future__ import annotations

import logging

from app.core.config import get_settings
from app.core.exceptions import EmailSendError

settings = get_settings()
logger = logging.getLogger(settings.NODE_LOGGER_NAME)


async def send_workflow_email(to: str, *, subject: str, text: str) -> None:
    """发一封工作流邮件。

    Raises:
        EmailSendError: 邮件机器人未初始化、连接失败或 SMTP 发送失败。
            ``BusinessException`` → 被 ``node_error_wrapper`` 转成 error 状态。
    """
    from utils.emailbot.mail_manager import get_mail_bot_

    email_bot = get_mail_bot_()
    try:
        if not email_bot.is_connected:
            await email_bot.connect()
        await email_bot.send_email(to=to, subject=subject, text=text)
    except EmailSendError:
        raise
    except Exception as e:
        logger.error("[mail] send failed | to=%s | subject=%s | error=%s", to, subject, e)
        raise EmailSendError(f"发送工作流邮件失败: {e}") from e
