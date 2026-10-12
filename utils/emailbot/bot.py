"""门面层：把配置/协议/解析/状态/规则组装成一个可用的邮件机器人。

对外只有三个动作：
- poll_once()  拉增量新邮件 → 存附件 → 跑规则 → 回复/提醒 → 更新游标
- send_email() 主动发信
- reply()      回复某封邮件（带正确的会话线程头）

与课程版的三处关键差异：
1. 回复前用 Message-ID 查重（state.seen_ids），游标回退也不会重复回信；
2. mark_seen 用 create_task 但保留任务引用 + done 回调清理，
   修复"裸 create_task 可能被 GC 回收导致任务没执行完"的经典坑；
3. 命中规则通过 on_match 回调通知业务方，"提醒"方式由外部决定
   （打日志 / 推 IM / 写数据库都行），机器人不关心。
"""
from __future__ import annotations

import asyncio
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Awaitable, Callable, Optional

from loguru import logger

from attachments import AttachmentSaver
from config import MailSettings
from imap_client import ImapClient
from models import ParsedEmail
from parser import parse_email
from rules import MailRule
from smtp_client import SmtpClient
from state import StateStore

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
        task.add_done_callback(self._bg_tasks.discard)

    # ---------- 收件与处理 ----------
    async def poll_once(self) -> list[ParsedEmail]:
        """一轮轮询：处理自上次游标之后的所有新邮件，返回处理过的邮件。"""
        await self._ensure_connected()

        last_uid = self._state.last_uid
        if last_uid is None:
            # 冷启动：把游标定到当前最大 UID，只处理"从现在起"的新邮件。
            # 若想补处理历史未读，把这里改成 fetch 条件 "UNSEEN"。
            self._state.set_last_uid(await self._imap.get_max_uid() or 0)
            logger.info("cold start, baseline last_uid={}", self._state.last_uid)
            return []

        uids = await self._imap.search_uids(f"UID {last_uid + 1}:*")
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
                logger.exception("handle email uid={} failed", uid)
            finally:
                # 无论成没成功都推进游标：失败已记日志，不会因此卡死整条链
                self._state.set_last_uid(int(uid))
                processed.append(email)

        logger.info("poll done: {} new email(s), last_uid={}", len(processed), self._state.last_uid)
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
        if matched is None:
            return

        logger.info("rule [{}] hit: uid={} from={} subject={}",
                    matched.name, email.uid, email.sender, email.subject)

        # 4) 去重后回复（Message-ID 判重，防游标回退导致的二次回复）
        if self.settings.auto_reply and matched.reply_text \
                and not self._state.is_seen(email.message_id):
            await self.reply(email, text=matched.reply_text)
        self._state.mark_seen(email.message_id)

        # 5) 通知业务方（提醒逻辑）
        if self.on_match:
            try:
                await self.on_match(email, matched)
            except Exception:
                logger.exception("on_match hook failed")

    async def fetch_latest(self, limit: int | None = None) -> list[ParsedEmail]:
        await self._ensure_connected()
        limit = limit or self.settings.fetch_limit
        uids = await self._imap.search_uids("ALL")
        uids = sorted(uids, key=lambda u: int(u) if u.isdigit() else 0)[-limit:]
        return [parse_email(u, m) for u, m in await self._imap.fetch_many(uids)]

    # ---------- 发件 ----------
    async def send_email(self, *, to: str | list[str], subject: str,
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
        logger.info("replied uid={} to={}", email.uid, reply_to)

        # 标记已读放后台（有引用保护），不阻塞轮询节奏
        if email.uid:
            self._spawn(self._imap.mark_seen(str(email.uid)))
