"""解析层：把 imaplib 取回的 MIME 对象拆成 ParsedEmail。

把附件完整提取出来，供上层落盘（简历抓取靠的就是这一步）。

中文主题 / 中文附件名遵循 RFC 2047，形如 =?utf-8?B?...?=，
必须先 decode_header 才能还原。
"""
from __future__ import annotations

import re
from datetime import datetime
from email.header import decode_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from typing import Optional

from models import Attachment, EmailAddress, ParsedEmail

# 一些客户端会把附件名报成这些"占位名"，此时按 content-type 猜扩展名
_PLACEHOLDER_NAMES = {"", "attachment", "noname", "n/a", "file"}
_TYPE_GUESS = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "text/plain": ".txt",
}


def decode_maybe(value: Optional[str]) -> str:
    """解 RFC 2047 编码的 header；对乱码 charset 做兜底，绝不抛异常。

    邮件头中的中文遵循 RFC 2047，形如 =?utf-8?5byg5LiJ?=。这个函数：
    用 decode_header() 拆分出 (字节串, charset) 对
    对 unknown-8bit / x-unknown 等乱码 charset 兜底为 utf-8
    对未知 charset（LookupError）兜底为 latin-1（1:1 字节映射，永远成功）
    绝不抛异常，保证解析不因编码问题中断
    """
    if not value:
        return ""
    out: list[str] = []
    for chunk, charset in decode_header(value):
        if isinstance(chunk, bytes):
            cs = (charset or "utf-8").lower()
            if cs in {"unknown-8bit", "x-unknown", "unknown"}:
                cs = "utf-8"
            try:
                out.append(chunk.decode(cs, errors="replace"))
            except LookupError:
                # latin-1 是 1:1 字节映射，永远成功
                out.append(chunk.decode("latin-1", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out).strip()


def parse_addresses(header: Optional[str]) -> list[EmailAddress]:
    """把一个邮件头（From / To / Cc）拆成结构化地址列表。

    头部的典型形态是 `"张三" <zhangsan@example.com>, "李四" <lisi@b.com>`，
    getaddresses 负责按 RFC 2822 语法切分并剥离尖括号——比手写正则可靠，
    能正确处理"显示名里带逗号"这类情况。
    """
    addrs: list[EmailAddress] = []
    # 传入 list 而非 str：getaddresses 的签名是"多个头的集合"，
    # 它会先把每条字符串里的折叠换行拆开再解析
    for name, addr in getaddresses([header or ""]):
        if not addr:
            continue   # 畸形头（只有名字没有地址）直接丢掉，不污染下游
        addrs.append(EmailAddress(name=decode_maybe(name), address=addr.strip()))
    return addrs



def strip_html(html: str) -> str:
    """无依赖的极简 HTML→文本。需要更准可换 BeautifulSoup。"""
    html = re.sub(r"<\s*br\s*/?>", "\n", html, flags=re.I)
    html = re.sub(r"</p\s*>", "\n\n", html, flags=re.I)
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.I | re.S)
    html = re.sub(r"<[^>]+>", "", html)
    return re.sub(r"\n{3,}", "\n\n", html).strip()


def _decode_payload(part: Message) -> str:
    raw = part.get_payload(decode=True)
    if raw is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _filename_of(part: Message) -> str:
    """取附件名：Content-Disposition filename 优先，退化到 Content-Type 参数，再兜底。"""
    raw_name = part.get_filename()
    if raw_name:
        name = decode_maybe(raw_name)
    else:
        # inline 图片/附件常把名字藏在 Content-Type 的 filename/name 参数里
        name = decode_maybe(part.get_param("filename") or part.get_param("name") or "")

    if name.lower() in _PLACEHOLDER_NAMES:
        ext = _TYPE_GUESS.get((part.get_content_type() or "").lower(), ".bin")
        name = f"attachment{ext}"
    return name


def _is_attachment(part: Message) -> bool:
    # 判断邮件里的某一个 MIME 部分（part）是不是附件
    disp = (part.get("Content-Disposition") or "").lower()
    if "attachment" in disp:
        return True
    # inline + 带文件名：QQ/微信邮箱发来的简历经常是这种
    if "inline" in disp and part.get_filename():
        return True
    # 无 Content-Disposition 的二进制部分（application/* / image/*）
    if not disp:
        ctype = (part.get_content_type() or "").lower()
        if ctype.startswith("application/") or ctype.startswith("image/"):
            return True
    return False


def extract_attachments(msg: Message) -> list[tuple[Attachment, bytes]]:
    """返回 (附件元信息, 原始字节) 列表；是否落盘由调用方决定。"""
    results: list[tuple[Attachment, bytes]] = []
    if not msg.is_multipart():
        return results

    for part in msg.walk():
        if part.is_multipart() or not _is_attachment(part):
            continue
        data = part.get_payload(decode=True) or b""
        att = Attachment(
            filename=_filename_of(part),
            saved_path=None,
            content_type=(part.get_content_type() or "application/octet-stream"),
            size=len(data),
        )
        results.append((att, data))
    return results


def parse_email(uid: str, msg: Message) -> ParsedEmail:
    subject = decode_maybe(msg.get("Subject"))

    from_list = parse_addresses(msg.get("From"))
    from_addr = from_list[0] if from_list else EmailAddress(name="", address="")

    date: Optional[datetime] = None
    try:
        if msg.get("Date"):
            date = parsedate_to_datetime(msg.get("Date"))
    except (TypeError, ValueError):
        date = None

    text_parts: list[str] = []
    html_parts: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart() or _is_attachment(part):
                continue
            ctype = (part.get_content_type() or "").lower()
            if ctype == "text/plain":
                text_parts.append(_decode_payload(part))
            elif ctype == "text/html":
                html_parts.append(_decode_payload(part))
    else:
        ctype = (msg.get_content_type() or "").lower()
        body = _decode_payload(msg)
        if ctype == "text/html":
            html_parts.append(body)
        else:
            text_parts.append(body)

    text = "\n".join(p.strip("\ufeff") for p in text_parts).strip()
    html = "\n".join(html_parts).strip()
    if not text and html:
        text = strip_html(html)

    # 注意：这里【不】提取附件元信息。parse_email 只负责解析头部/正文，
    # 附件提取统一由 AttachmentSaver.save_all() 完成，避免对同一 MIME 树
    # 重复 walk()（parse_email 和 save_all 各遍历一次是浪费）。
    # ParsedEmail.attachments 在 save_all() 调用后由 bot._handle_one() 回填。
    return ParsedEmail(
        uid=str(uid),
        message_id=(msg.get("Message-ID") or "").strip(),
        subject=subject,
        from_=from_addr,
        to=parse_addresses(msg.get("To")),
        cc=parse_addresses(msg.get("Cc")),
        date=date,
        text=text,
        html=html,
        raw=msg,
    )
