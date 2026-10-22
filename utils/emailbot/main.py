"""入口：轮询循环 + 提醒回调示例。

运行前设环境变量（授权码，不是登录密码）：
    $env:MAIL_USERNAME = "you@qq.com"
    $env:MAIL_PASSWORD = "你的16位授权码"
    python main.py
"""
from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv
from loguru import logger

from bot import MailBot
from config import MailSettings
from models import ParsedEmail
from rules import DEFAULT_RULES


async def notify(email: ParsedEmail, rule) -> None:
    """提醒回调：命中规则时该做什么。示例只打日志，可自行换成
    企业微信/钉钉 webhook、写数据库、推前端 WebSocket 等。"""
    files = "、".join(a.filename for a in email.attachments) or "（无附件）"
    logger.info(
        "[命中规则] {} | 来自 {} | 主题: {} | 附件: {}",
        rule.name, email.from_.address, email.subject, files,
    )


async def main() -> None:
    # 启动时加载 .env 文件中的变量到环境变量，方便本地开发。
    # 若系统环境变量已设置，load_dotenv 默认不会覆盖（override=False）。
    load_dotenv()

    # 凭据从环境变量读取：绝不要把授权码硬编码进源码（会随代码泄露）。
    # 少了这两个变量，MailSettings 的必填校验会立刻报错，fail fast。
    settings = MailSettings(
        imap_host="imap.163.com",
        smtp_host="smtp.163.com",
        smtp_port=465,
        smtp_starttls=False,
        smtp_implicit_ssl=True,
        email=os.getenv("MAIL_USERNAME"),
        password=os.getenv("MAIL_PASSWORD"),
        poll_interval=20,
        attach_dir="./attachments",
        state_file="./mailbot_state.json",
    )

    # on_match 是"提醒"的挂钩点：规则命中后机器人回调这里，具体形态（日志/
    # 钉钉 webhook/写库）完全由业务方决定，机器人本身不关心
    bot = MailBot(settings, rules=DEFAULT_RULES, on_match=notify)
    async with bot:  # 进语境自动 connect+login，出语境自动 logout+quit
        logger.info("mail bot started, polling every {}s ...", settings.poll_interval)
        while True:
            try:
                await bot.poll_once()
            except Exception:
                # 单轮崩溃（网络抖动/解析异常）不退出进程，下一轮靠断线自愈恢复
                logger.exception("poll round crashed, will retry next tick")
            await asyncio.sleep(settings.poll_interval)






if __name__ == "__main__":
    asyncio.run(main())
