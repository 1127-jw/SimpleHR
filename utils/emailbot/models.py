"""数据结构层：机器人内部流转的"类型化邮件"。

为什么要自己定义这三个类：email.message.Message 是"半成品"——取一个主题要
判断编码、取正文要遍历 MIME 树。转成下面的 dataclass 后，下游拿到的就是干净的
字符串和列表，业务代码不必再碰任何 MIME 细节。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from email.message import Message
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class EmailAddress:
    """一个发/收件人。frozen=True 使其可哈希，便于将来当作去重 key 使用。"""
    name: str        # 显示名，如 "张三"（已解码，可能是中文）
    address: str     # 纯邮箱地址，如 "zhangsan@example.com"


@dataclass(frozen=True)
class Attachment:
    """一个附件的元信息。原始字节不放进这个对象，避免整封邮件体积常驻内存。"""
    filename: str                 # 已解码的真实文件名（可能是中文名）
    saved_path: Optional[Path]    # 落盘路径；刚解析出来时为 None，落盘失败时仍为 None
    content_type: str             # MIME 类型，如 application/pdf
    size: int                     # 字节数


@dataclass
class ParsedEmail:
    """一封邮件解析后的完整结果，贯穿"抓附件→跑规则→回复→提醒"全流程。"""
    uid: str                       # IMAP UID：服务器分配的永久编号，增量游标靠它
    message_id: str                # 邮件全局唯一 ID（带尖括号），跨设备稳定，去重靠它
    subject: str                   # 已解码的主题
    from_: EmailAddress            # from 是 Python 关键字，尾部加下划线规避
    to: list[EmailAddress] = field(default_factory=list)
    cc: list[EmailAddress] = field(default_factory=list)
    date: Optional[datetime] = None    # 日期解析失败置 None，不让整封邮件作废
    text: str = ""                 # 纯文本正文（原始邮件没有 text 时由 HTML 转换兜底）
    html: str = ""                 # 原始 HTML 正文，可为空
    attachments: list[Attachment] = field(default_factory=list)
    raw: Optional[Message] = None  # 原始 MIME 对象：reply() 要从这里取 Reply-To / References

    # 注意：本类【不能】加 frozen=True —— _handle_one() 会用"落盘后的附件列表"
    # 覆盖 attachments 字段，需要可变。另外 list 字段即便是 frozen 也只锁顶层引用，
    # 内部元素本来就可变，别指望 frozen 提供深层不可变。

    @property
    def sender(self) -> str:
        """小写化的发件地址。邮箱地址大小写不敏感，统一后再和"我自己"比较。"""
        return self.from_.address.lower()
