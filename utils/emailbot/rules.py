"""规则层：关键词监听 + 命中判定。

设计成"条件 → 动作"的小结构体，业务方只需往列表里加规则，
不用改机器人主体。这是把"自动回信"从写死文案升级为可配置分发的关键。

匹配域（field）：
- subject   主题
- from      发件人（含显示名与地址）
- body      正文（text，缺失时 parser 已用 html 兜底）
- attachment 附件文件名（简历场景常用）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

MatchField = Literal["subject", "from", "body", "attachment"]


@dataclass
class MailRule:
    name: str                                  # 规则名，用于日志
    keywords: list[str]                        # 任一命中即算命中（OR）
    fields: list[MatchField] = field(default_factory=lambda: ["subject", "body"])
    case_sensitive: bool = False
    require_attachment: bool = False           # 必须带附件才算命中（"投了简历"）
    reply_text: str | None = None              # 命中后的回复文案；None 表示只提醒不回信
    catch_all: bool = False                    # 保底规则：未命中其他规则时触发

    def matches(self, subject: str, sender: str, body: str,
                attachment_names: list[str]) -> bool:
        # 保底规则：直接命中所有邮件
        if self.catch_all:
            return True

        if self.require_attachment and not attachment_names:
            return False

        # 把可匹配的四个区域先摊平成一个字典：规则只声明"看哪几块"（fields），
        # 将来新增匹配域（如 date）只要在这里加一项，判定循环不用动
        haystack = {
            "subject": subject,
            "from": sender,
            "body": body,
            "attachment": " ".join(attachment_names),   # 多个附件名拼成一串一起搜
        }
        for f in self.fields:
            text = haystack.get(f, "") or ""
            if not self.case_sensitive:
                # 大小写不敏感时两边都转小写比较（"CV"/"Resume" 都能命中）
                text = text.lower()
                words = [k.lower() for k in self.keywords]
            else:
                words = self.keywords
            # 子串包含匹配（不是分词/正则）：够用、快、不会误伤，
            # 需要更精细规则时把这里换成 re.search 即可
            if any(w in text for w in words):
                return True
        return False


# 招聘场景的默认规则示例
DEFAULT_RULES = [
    MailRule(
        name="简历投递",
        keywords=["简历", "应聘", "求职", "投递", "resume", "cv"],
        fields=["subject", "body", "attachment"],
        require_attachment=True,
        reply_text="您好，您的简历已收到，我们会在评估后与您联系。",
    ),
    MailRule(
        name="询问面试时间或进度",
        keywords=[
            "面试时间", "面试安排", "面试进度", "什么时候面试",
            "有结果了吗", "安排面试", "去面试", "来面试",
            "面试邀请", "协商面试", "确认面试", "调整面试",
            # 候选人常见回复（简短确认/改期）
            "确认", "好的", "可以", "方便", "收到", "明白",
            "准时参加", "会参加", "没问题", "同意",
            "不方便", "换时间", "改时间", "另约", "重新约",
            "上午", "下午", "晚上", "点", "分",
        ],
        fields=["subject", "body"],
        reply_text="您好，您的应聘状态正在评估中，请耐心等待通知。",
    ),
    # 保底规则：放在最后，未命中其他规则时触发
    MailRule(
        name="其他邮件",
        keywords=[],
        fields=[],
        catch_all=True,
        reply_text="您好，您的邮件已收到，我们会尽快处理。",
    ),
]
