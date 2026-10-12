"""附件落盘层：把解析出的附件字节写到本地目录（简历抓取的最后一步）。

安全与健壮性要点：
1. 文件名清洗：邮件附件名不可信，可能含路径分隔符或非法字符
   （如 "..\\..\\evil.pdf"），必须剥离目录部分并过滤 Windows 非法字符，
   否则会写出目标目录（路径穿越）。
2. 防覆盖：不同候选人可能都上传"简历.pdf"，用 "{uid}_{序号}_{原名}" 命名。
3. 只保存引用信息，字节不长期驻留内存对象里。
"""
from __future__ import annotations

import re
from pathlib import Path

from loguru import logger

from models import Attachment, ParsedEmail
from parser import extract_attachments

# Windows/Linux 都安全的字符集：中文保留，只滤掉路径与非法符号
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_filename(name: str) -> str:
    """把不可信的附件名洗成能安全落盘的文件名。

    例：r"..\\..\\windows\\evil.pdf" → "__.._windows_evil.pdf"
    反斜杠被换成下划线后它就不再是"路径"，只是一个奇怪点的名字，
    从而永远不可能跳出我们的附件目录。
    """
    name = _UNSAFE.sub("_", name).strip().strip(".")
    return name[:120] or "attachment"  # 防止空名和超长名（Windows 路径有 260 限制）


class AttachmentSaver:
    def __init__(self, base_dir: str | Path):
        self._base = Path(base_dir)
        # exist_ok=True：目录已存在时不报错，机器人重启可反复初始化
        self._base.mkdir(parents=True, exist_ok=True)

    def save_all(self, email: ParsedEmail) -> list[Attachment]:
        """把一封邮件的所有附件落盘，返回带 saved_path 的附件列表。"""
        if email.raw is None:
            return []

        saved: list[Attachment] = []
        # 每封邮件建一个子目录，避免混在一起
        mail_dir = self._base / safe_filename(f"{email.uid}")
        mail_dir.mkdir(parents=True, exist_ok=True)

        # extract_attachments 返回 (元信息, 原始字节) 列表；字节在这一步已被
        # get_payload(decode=True) 解码（base64 → 原始 PDF 字节），可直接写盘
        for idx, (att, data) in enumerate(extract_attachments(email.raw), start=1):
            filename = safe_filename(att.filename)
            # 序号前缀 {idx:02d}：一封邮件里两个同名附件也不会互相覆盖
            path = mail_dir / f"{idx:02d}_{filename}"
            try:
                path.write_bytes(data)
            except OSError as e:
                logger.error("save attachment failed: {} ({})", path, e)
                continue
            saved.append(Attachment(
                filename=filename,
                saved_path=path,
                content_type=att.content_type,
                size=att.size,
            ))
            logger.info("saved attachment: {} ({} bytes)", path, att.size)
        return saved
