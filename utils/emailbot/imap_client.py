"""IMAP 收件层。

两个关键工程点：
1. imaplib 是阻塞库 → 每个调用用 asyncio.to_thread 丢线程池，不卡事件循环。
2. imaplib 不是线程安全的 → 用 asyncio.Lock 把所有操作串行化。


所有操作走 UID 而不是序号：UID 单调递增、永久稳定，是增量游标的基础。
"""
from __future__ import annotations

import asyncio
import imaplib
from email import message_from_bytes
from email.message import Message
from typing import Iterable, Optional

from loguru import logger

# 注册 IMAP ID 命令（RFC 2971）到 imaplib 的命令表。
# 网易系（163/126/yeah.net）会检查客户端是否发送过 ID，
# 没发则在 SELECT 阶段返回 "Unsafe Login. Please contact kefu_service163"。
# 注册后 imaplib 在 AUTH 状态下允许发送 ID 命令，避免被误判为非法命令。
imaplib.Commands["ID"] = ("AUTH",)


class ImapClient:
    def __init__(self, host: str, port: int = 993, *, ssl: bool = True):
        self._host = host
        self._port = port
        self._ssl = ssl
        self._conn: Optional[imaplib.IMAP4] = None
        self._lock = asyncio.Lock()          # 串行化所有 IMAP 操作
        self._username: Optional[str] = None  # 保存凭据供重连使用
        self._password: Optional[str] = None
        self._mailbox: Optional[str] = None

    async def connect(self) -> None:
        def _connect():
            if self._ssl:
                return imaplib.IMAP4_SSL(self._host, self._port)
            return imaplib.IMAP4(self._host, self._port)

        async with self._lock:
            self._conn = await asyncio.to_thread(_connect)

    async def login(self, username: str, password: str) -> None:
        async with self._lock:
            self._require_conn()
            await asyncio.to_thread(self._conn.login, username, password)
            self._username, self._password = username, password
            await asyncio.to_thread(self._send_id)

    def _send_id(self) -> None:
        """登录后发送 IMAP ID 命令（RFC 2971）自报客户端身份。

        为什么必须发：网易系（163/126/yeah.net）会检查第三方客户端是否
        发送过 ID，没发则在 SELECT 阶段返回 "Unsafe Login. Please contact
        kefu_service163"。QQ/Gmail 不强制校验，此命令对它们是良性无害的。

        实现说明：imaplib 没有公开的 ID 方法（它只实现到 IMAP4rev1，
        ID 属扩展协议），这里借用内部 _simple_command 手工拼命令行——
        这是社区通行做法；服务端不支持 ID 时抛异常会被捕获，仅记警告。
        """
        args = '("name" "my_mailbot" "version" "1.0" "vendor" "my_mailbot")'
        try:
            self._conn._simple_command("ID", args)
        except Exception as e:
            logger.warning("IMAP ID command failed (non-fatal): {}", e)

    async def select(self, mailbox: str = "INBOX") -> None:
        async with self._lock:
            self._require_conn()
            await asyncio.to_thread(self._conn.select, mailbox)
            self._mailbox = mailbox

    async def logout(self) -> None:
        async with self._lock:
            if self._conn:
                try:
                    await asyncio.to_thread(self._conn.logout)
                except Exception:
                    pass
                finally:
                    self._conn = None

    def _require_conn(self) -> None:
        if not self._conn:
            raise RuntimeError("IMAP not connected")

    def _require_state(self, expected: str) -> None:
        """校验连接当前状态；不在预期状态时抛错，由调用方决定重连还是重新 SELECT。"""
        if not self._conn:
            raise RuntimeError("IMAP not connected")
        state = getattr(self._conn, "state", None)
        if state != expected:
            raise imaplib.IMAP4.error(
                f"command illegal in state {state}, only allowed in states {expected}"
            )

    async def _ensure_selected(self) -> None:
        """确保连接处于 SELECTED 状态。

        注意：imaplib.IMAP4.select() 在命令失败时【不抛异常】，而是把
        self.state 重置为 'AUTH' 并静默返回。所以必须检查调用后的状态。
        """
        if not self._conn:
            raise RuntimeError("IMAP not connected")
        if getattr(self._conn, "state", None) == "SELECTED":
            return

        mailbox = self._mailbox or "INBOX"
        logger.info("IMAP state is {}, selecting {}...", self._conn.state, mailbox)
        await asyncio.to_thread(self._conn.select, mailbox)
        self._mailbox = mailbox

        # select() 失败时 imaplib 只把 state 设为 AUTH，不抛异常
        if getattr(self._conn, "state", None) != "SELECTED":
            logger.warning(
                "IMAP select {} failed (state={}), reconnecting without ID",
                mailbox, self._conn.state,
            )
            # 跳过 _send_id：之前 _send_id 失败很可能就是导致 select 被服务器拒绝的原因
            await self._reconnect(skip_id=True)

    async def _reconnect(self, skip_id: bool = False) -> None:
        """断线自愈：重连 + 重新登录 + 重新选箱（调用方必须已持有锁）。

        skip_id=True 时跳过 _send_id，用于 _send_id 本身导致服务器拒绝后续命令的场景。
        """
        logger.info("IMAP reconnecting (skip_id={})...", skip_id)
        if self._conn:
            try:
                await asyncio.to_thread(self._conn.logout)
            except Exception:
                pass
            self._conn = None
        conn = imaplib.IMAP4_SSL(self._host, self._port) if self._ssl \
            else imaplib.IMAP4(self._host, self._port)
        self._conn = conn
        if self._username and self._password:
            await asyncio.to_thread(conn.login, self._username, self._password)
            if not skip_id:
                # 重连后是全新连接，网易的 ID 检查同样适用，必须补发；
                # 这里不能调 self.login()——它内部又拿 self._lock，会自锁死
                await asyncio.to_thread(self._send_id)
            # 无论 self._mailbox 是否已设置，都必须重新 SELECT 才能恢复可用状态
            mailbox = self._mailbox or "INBOX"
            await asyncio.to_thread(conn.select, mailbox)
            self._mailbox = mailbox

    async def search_uids(self, criteria: str, *, min_uid: int = 0) -> list[str]:
        """按条件搜 UID。criteria 例："ALL" / "UNSEEN" / f"UID {n}:*"

        min_uid > 0 时会在客户端侧额外过滤掉 UID <= min_uid 的结果，
        用于规避部分 IMAP 服务器（如 QQ 邮箱）UID 范围搜索返回错误结果的问题。
        """
        def _search():
            typ, data = self._conn.uid("SEARCH", criteria)
            logger.debug("IMAP SEARCH cmd=UID SEARCH {} → typ={}, data={}", criteria, typ, data)
            if typ != "OK":
                raise RuntimeError(f"IMAP SEARCH failed: {typ} {data}")
            # 空邮箱或没匹配时 data[0] 是 b""，所以要 `or b""` 兜底再 split
            raw = data[0] or b""
            uids = [x.decode() if isinstance(x, bytes) else str(x) for x in raw.split() if x]
            # 客户端侧过滤：部分服务器（QQ 邮箱）UID 范围搜索可能返回不符合范围的结果
            if min_uid > 0:
                uids = [u for u in uids if u.isdigit() and int(u) > min_uid]
            return uids

        async with self._lock:
            self._require_conn()
            await self._ensure_selected()
            try:
                return await asyncio.to_thread(_search)
            except (imaplib.IMAP4.abort, imaplib.IMAP4.error) as e:
                logger.warning("SEARCH aborted, reconnect & retry: {}", e)
                await self._reconnect()
                await self._ensure_selected()
                return await asyncio.to_thread(_search)

    async def fetch_rfc822(self, uid: str) -> Message:
        """按 UID 取整封邮件（含附件），返回未解析的 MIME 对象。"""
        def _fetch():
            # "(RFC822)" 是 FETCH 的宏，等价于一次取回头部+全部 body part，
            # 即完整的原始邮件字节；只想取标题时可用 "(BODY[HEADER])" 省流量
            typ, data = self._conn.uid("FETCH", str(uid), "(RFC822)")
            if typ != "OK":
                raise RuntimeError(f"IMAP FETCH failed: {typ} {data}")
            # data 形态是混合列表：[b"42 (FLAGS ... {1234} bytes ...", (b"...", <邮件字节>)]
            # 真正的邮件内容只在"二元组"元素的第二项里，所以要遍历找 tuple
            for item in data:
                if isinstance(item, tuple) and len(item) >= 2:
                    return message_from_bytes(item[1])
            raise RuntimeError("IMAP FETCH returned no message")

        async with self._lock:
            self._require_conn()
            await self._ensure_selected()
            try:
                return await asyncio.to_thread(_fetch)
            except (imaplib.IMAP4.abort, imaplib.IMAP4.error) as e:
                logger.warning("FETCH uid={} aborted, reconnect & retry: {}", uid, e)
                await self._reconnect()
                await self._ensure_selected()
                return await asyncio.to_thread(_fetch)

    async def fetch_many(self, uids: Iterable[str]) -> list[tuple[str, Message]]:
        """逐封取；单封失败只记日志，不影响整批。"""
        out: list[tuple[str, Message]] = []
        for uid in uids:
            try:
                out.append((str(uid), await self.fetch_rfc822(uid)))
            except Exception:
                logger.exception("fetch failed uid={}", uid)
        return out

    async def get_max_uid(self) -> Optional[int]:
        """取邮箱里当前最大的 UID，用作冷启动基线（比它大的才是新信）。"""
        uids = await self.search_uids("ALL")
        if not uids:
            return None
        try:
            return max(int(x) for x in uids)
        except ValueError:
            return None

    async def mark_seen(self, uid: str) -> None:
        """把某封邮件标记为已读（\\Seen flag）。"""
        def _store():
            # STORE 的 "+FLAGS" 是"追加"语义（对应 "-FLAGS" 移除、"FLAGS" 覆盖）。
            # \Seen 就是"已读"标志，加上后收件箱里不再显示为未读。
            # 前面加 r"" 是因为反斜杠 S 在普通字符串里会被当作转义起始。
            typ, data = self._conn.uid("STORE", str(uid), "+FLAGS", r"(\Seen)")
            if typ != "OK":
                raise RuntimeError(f"STORE \\Seen failed: {typ} {data}")

        async with self._lock:
            self._require_conn()
            await self._ensure_selected()
            try:
                await asyncio.to_thread(_store)
            except (imaplib.IMAP4.abort, imaplib.IMAP4.error) as e:
                logger.warning("STORE uid={} aborted, reconnect & retry: {}", uid, e)
                await self._reconnect()
                await self._ensure_selected()
                await asyncio.to_thread(_store)
