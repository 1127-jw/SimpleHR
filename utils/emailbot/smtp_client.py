"""SMTP 发件层。

流程：connect(ehlo → starttls → ehlo) → login → send_message → quit。
timeout=30 防止发信时网络卡死把整条链路挂住。
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage
from typing import Optional

logger = logging.getLogger(__name__)


class SmtpClient:
    def __init__(self, host: str, port: int = 587, *, starttls: bool = True,
                 implicit_ssl: bool = False):
        """两种加密建连模式，按 implicit_ssl 切换：

        - implicit_ssl=False（默认）：587 提交端口，先明文建连、再用
          STARTTLS 升级为加密通道（经典 ehlo→starttls→ehlo 流程）。
        - implicit_ssl=True：465/994 端口，TCP 一连上就套 TLS（SMTP_SSL），
          没有明文阶段，也不需要 STARTTLS。网易系（163/126）官方推荐这种。
        """
        self._host = host
        self._port = port
        self._starttls = starttls
        self._implicit_ssl = implicit_ssl
        self._conn: Optional[smtplib.SMTP] = None
        self._lock = asyncio.Lock()
        self._username: Optional[str] = None
        self._password: Optional[str] = None

    def _open(self) -> smtplib.SMTP:
        """建立连接（同步方法，由外层 asyncio.to_thread 调用）。"""
        if self._implicit_ssl:
            # 465 隐式 SSL：SMTP_SSL 在返回前已完成 TLS 握手，
            # 之后的 ehlo/login/send 全程都在加密通道上进行
            conn = smtplib.SMTP_SSL(self._host, self._port, timeout=30)
            conn.ehlo()
            return conn

        # 587 STARTTLS 模式。为什么是 ehlo → starttls → ehlo 两次 ehlo：
        # 1. 第一次 ehlo 协商扩展能力，服务端此时才允许 STARTTLS 命令；
        # 2. starttls() 把明文通道升级成 TLS 加密通道；
        # 3. 加密后必须重新 ehlo —— 升级前协商的明文能力（尤其 AUTH 登录）
        #    不能复用，跳过这一步会报 "SMTPAUTH not supported" 之类的错。
        conn = smtplib.SMTP(self._host, self._port, timeout=30)
        conn.ehlo()
        if self._starttls:
            conn.starttls()
            conn.ehlo()
        return conn

    async def connect(self) -> None:
        async with self._lock:
            self._conn = await asyncio.to_thread(self._open)

    async def login(self, username: str, password: str) -> None:
        async with self._lock:
            if not self._conn:
                raise RuntimeError("SMTP not connected")
            await asyncio.to_thread(self._conn.login, username, password)
            self._username, self._password = username, password

    async def send_message(self, msg: EmailMessage) -> None:
        """发送；连接已断时重登并重试一次。

        注意：若断连发生在服务端已受理之后，重试会让对方收到两封。
        对重复敏感的场景，应在上层用 message_id 做幂等。
        """
        async with self._lock:
            if not self._conn:
                raise RuntimeError("SMTP not connected")
            try:
                # send_message 会自动把 msg 的 From 头用作信封发件人，
                # 并把收件人从 To/Cc 头里收集起来，不用手工拼 RCPT TO
                await asyncio.to_thread(self._conn.send_message, msg)
                return
            except smtplib.SMTPServerDisconnected as e:
                # 网络层断开（连接被服务端超时踢掉）：属于可恢复错误，重连重试
                logger.warning("SMTP disconnected, reconnecting & retrying: %s", e)
            except smtplib.SMTPException as e:
                # 5xx 之类是硬性错误（认证失败/收件人不存在/内容被拒），
                # 重试无意义，直接抛给上层
                logger.error("SMTP rejected message: %s", e)
                raise

            await self._reconnect_locked()
            await asyncio.to_thread(self._conn.send_message, msg)

    async def _reconnect_locked(self) -> None:
        """重连（调用方已持有锁）。"""
        if self._conn:
            try:
                await asyncio.to_thread(self._conn.quit)
            except Exception:
                pass
            self._conn = None
        self._conn = await asyncio.to_thread(self._open)
        if self._username and self._password:
            await asyncio.to_thread(self._conn.login, self._username, self._password)

    async def quit(self) -> None:
        async with self._lock:
            if not self._conn:
                return
            try:
                await asyncio.to_thread(self._conn.quit)
            finally:
                self._conn = None
