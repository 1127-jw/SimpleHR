"""门面层：把配置/协议/解析/状态/规则组装成一个可用的邮件机器人。

对外只有三个动作：
- poll_once()  拉增量新邮件 → 存附件 → 跑规则 → 回复/提醒 → 更新游标
- send_email() 主动发信
- reply()      回复某封邮件（带正确的会话线程头）

on_match 邮件规则命中后执行的回调动作 可以换成企业微信/钉钉 webhook，或写库后推前端


属性	                    说明
bot.is_connected	    是否已连接（bool）
bot.settings	        MailSettings 实例
bot.rules	            规则列表
bot._state.last_uid	    当前游标 UID
bot._state.is_seen(msg_id)	某邮件是否已处理
bot._bg_tasks	        正在运行的后台任务集合

poll_once() — 一轮完整处理：
    1. 读 last_uid（从 mailbot_state.json）
       ├─ None → 冷启动，设 last_uid = 当前最大 UID，返回 []
       └─ 有值 → 搜索 UID > last_uid 的新邮件

    2. 对每封新邮件:
       ├─ parse_email() → 结构化解析
       ├─ save_all() → 附件落盘
       ├─ mark_seen() → 标记已读（IMAP \Seen + state 去重）
       ├─ 遍历 rules 匹配关键词
       │   ├─ 命中 → reply() 自动回复（如果 auto_reply=True）
       │   └─ on_match() 通知回调（日志/钉钉/Webhook）
       └─ 更新 last_uid（无论成功失败都推进）


"""
from __future__ import annotations

import asyncio
import logging
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Awaitable, Callable, Optional

from utils.emailbot.attachments import AttachmentSaver
from utils.emailbot.config import MailSettings
from utils.emailbot.imap_client import ImapClient
from utils.emailbot.models import ParsedEmail
from utils.emailbot.parser import parse_email
from utils.emailbot.rules import MailRule
from utils.emailbot.smtp_client import SmtpClient
from utils.emailbot.state import StateStore

logger = logging.getLogger(__name__)

# 命中规则后的回调：async def hook(email, rule) -> None
MatchHook = Callable[[ParsedEmail, MailRule], Awaitable[None]]


class MailBot:
    def __init__(self, settings: MailSettings,
                 rules: list[MailRule],
                 on_match: Optional[MatchHook] = None):
        self.settings = settings
        self.rules = rules
        self.on_match = on_match
        self._imap = ImapClient(settings.imap_host, settings.imap_port, ssl=settings.imap_ssl)
        # implicit_ssl 透传：QQ 走默认 587+STARTTLS；163 设 465+implicit 走 SMTP_SSL
        self._smtp = SmtpClient(settings.smtp_host, settings.smtp_port,
                                starttls=settings.smtp_starttls,
                                implicit_ssl=settings.smtp_implicit_ssl)
        self._state = StateStore(settings.state_file)
        self._saver = AttachmentSaver(settings.attach_dir)
        self.is_connected = False
        self._bg_tasks: set[asyncio.Task] = set()   # 持有引用，防 GC

    # ---------- 生命周期 ----------
    async def __aenter__(self) -> "MailBot":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def connect(self) -> None:
        await self._imap.connect()
        await self._imap.login(self.settings.email, self.settings.password)
        await self._imap.select(self.settings.imap_mailbox)
        await self._smtp.connect()
        await self._smtp.login(self.settings.email, self.settings.password)
        self.is_connected = True

    async def close(self) -> None:
        # 等后台 mark_seen 收尾（最多 5 秒），再断开连接
        if self._bg_tasks:
            done, pending = await asyncio.wait(self._bg_tasks, timeout=5)
            # 超时未完成的任务必须取消，否则事件循环关闭时会报
            # "Task was destroyed but it is pending"
            for task in pending:
                task.cancel()
            # 让取消传播，避免 "Task exception was never retrieved" 警告
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        await self._imap.logout()
        await self._smtp.quit()
        self.is_connected = False

    async def _ensure_connected(self) -> None:
        if not self.is_connected:
            await self.connect()

    def _spawn(self, coro) -> None:
        """发射后台任务：保留引用 + 完成即移除 + 异常记录。"""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        # add_done_callback 是 asyncio.Task 的方法，用于在任务完成时自动执行一个回调函数。
        task.add_done_callback(self._bg_tasks.discard)

    # ---------- 收件与处理 ----------
    async def poll_once(self) -> list[ParsedEmail]:
        """一轮轮询：处理自上次游标之后的所有新邮件，返回处理过的邮件。"""
        await self._ensure_connected()

        last_uid = self._state.last_uid
        logger.info("poll start: last_uid=%s", last_uid)
        if last_uid is None:
            # 冷启动：把游标定到当前最大 UID，只处理"从现在起"的新邮件。
            # 若想补处理历史未读，把这里改成 fetch 条件 "UNSEEN"。
            self._state.set_last_uid(await self._imap.get_max_uid() or 0)
            logger.info("cold start, baseline last_uid=%s", self._state.last_uid)
            return []

        uids = await self._imap.search_uids(f"UID {last_uid + 1}:*", min_uid=last_uid)
        logger.info("poll search: last_uid=%s, found %s uid(s) %s", last_uid, len(uids), uids)
        if not uids:
            return []

        processed: list[ParsedEmail] = []
        ordered = sorted(uids, key=lambda u: int(u) if u.isdigit() else 0)
        # 一次批量取回全部新邮件，再按 UID 升序逐封处理
        fetched = {u: m for u, m in await self._imap.fetch_many(ordered)}
        for uid in ordered:
            raw = fetched.get(uid)
            if raw is None:
                continue
            email = parse_email(uid, raw)

            try:
                await self._handle_one(email)
            except Exception:
                logger.exception("handle email uid=%s failed", uid)
            finally:
                # 无论成没成功都推进游标：失败已记日志，不会因此卡死整条链
                self._state.set_last_uid(int(uid))
                processed.append(email)

        logger.info("poll done: %s new email(s), last_uid=%s", len(processed), self._state.last_uid)
        return processed

    async def _handle_one(self, email: ParsedEmail) -> None:
        # 1) 不回自己（自动回复死循环的头号来源）
        if email.sender == self.settings.email.lower():
            return

        # 2) 抓附件（简历落盘）
        attachments = self._saver.save_all(email)
        email.attachments = attachments

        # 3) 规则匹配
        matched: Optional[MailRule] = None
        for rule in self.rules:
            if rule.matches(subject=email.subject,
                            sender=f"{email.from_.name} {email.from_.address}",
                            body=email.text,
                            attachment_names=[a.filename for a in attachments]):
                matched = rule
                break

        # 4) 无论规则是否匹配，只要处理过就标记已读（IMAP \Seen + state 去重）
        #    这样不会重复处理同一封邮件，也不会在邮箱里一直显示未读
        await self._mark_email_seen(email)

        if matched is None:
            return

        logger.info("rule [%s] hit: uid=%s from=%s subject=%s",
                    matched.name, email.uid, email.sender, email.subject)

        # 5) 去重后回复（Message-ID 判重，防游标回退导致的二次回复）
        if self.settings.auto_reply and matched.reply_text \
                and not self._state.is_seen(email.message_id):
            await self.reply(email, text=matched.reply_text)
        self._state.mark_seen(email.message_id)

        # 6) 通知业务方（提醒逻辑）
        if self.on_match:
            try:
                await self.on_match(email, matched)
            except Exception:
                logger.exception("on_match hook failed")

    async def _mark_email_seen(self, email: ParsedEmail) -> None:
        """把一封邮件标记为已读：IMAP \\Seen 标志 + state 去重。"""
        # state 去重（防同 UID 回退导致的重复处理）
        logger.debug("mark_seen uid=%s message_id=%s is_seen=%s",
                     email.uid, email.message_id, self._state.is_seen(email.message_id))
        self._state.mark_seen(email.message_id)
        # IMAP \Seen 标志（让邮箱里不再显示为未读）
        if email.uid:
            try:
                await self._imap.mark_seen(str(email.uid))
            except Exception:
                logger.exception("mark_seen uid=%s failed", email.uid)

    async def fetch_latest(self, limit: int | None = None) -> list[ParsedEmail]:
        await self._ensure_connected()
        limit = limit or self.settings.fetch_limit
        uids = await self._imap.search_uids("ALL")
        uids = sorted(uids, key=lambda u: int(u) if u.isdigit() else 0)[-limit:]
        return [parse_email(u, m) for u, m in await self._imap.fetch_many(uids)]

    # ---------- 发件 ----------
    async def send_email(self,
                         *,
                         to: str | list[str],
                         subject: str,
                         text: str, html: str | None = None,
                         cc: str | list[str] | None = None) -> None:
        await self._ensure_connected()
        msg = EmailMessage()
        msg["From"] = self.settings.email
        msg["To"] = ", ".join(to) if isinstance(to, list) else to
        if cc:
            msg["Cc"] = ", ".join(cc) if isinstance(cc, list) else cc
        msg["Subject"] = subject
        # 自己生成 Message-ID：不生成的话部分服务端会补，但格式不受控；
        # 而后续别人回我们这封信时，reply() 要靠它做会话线程
        msg["Message-ID"] = make_msgid()
        # set_content 会按文本内容自动选定传输编码（中文一般走 base64），
        # 不需要手工处理编码问题
        msg.set_content(text)
        if html:
            # add_alternative 把邮件变成 multipart/alternative：同时含纯文本
            # 与 HTML 两个版本，收件端按自身能力挑一个显示（不支持 HTML 的
            # 客户端就回落到上面的 text）
            msg.add_alternative(html, subtype="html")
        await self._smtp.send_message(msg)

    async def reply(self, email: ParsedEmail, *, text: str) -> None:
        raw = email.raw
        if raw is None:
            raise ValueError("ParsedEmail.raw missing, cannot reply")

        # Reply-To 优先：转发邮件才不会回错给转发人
        reply_to = (raw.get("Reply-To") or "").strip() or email.from_.address
        if not reply_to:
            raise ValueError("cannot reply: no sender address")

        subject = email.subject
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        msg = EmailMessage()
        msg["From"] = self.settings.email
        msg["To"] = reply_to
        msg["Subject"] = subject
        msg["Message-ID"] = make_msgid()
        if email.message_id:
            # 这两个头让对方客户端把回信挂到同一会话，而不是新邮件
            msg["In-Reply-To"] = email.message_id
            refs = (raw.get("References") or "").strip()
            msg["References"] = f"{refs} {email.message_id}".strip() or email.message_id
        msg.set_content(text)
        await self._smtp.send_message(msg)
        logger.info("replied uid=%s to=%s", email.uid, reply_to)
