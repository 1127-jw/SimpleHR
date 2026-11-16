"""规则层（L0 确定性预过滤）：只判「要不要进 LLM」，不判「邮件是什么意思」。

三种动作：
- ``BLOCK``：拦截，直接忽略（自动回复 / 退信 / 营销 / 黑名单）
- ``ALLOW``：放行，不附标签
- ``PRIOR``：放行 + 打先验意图标签（仅影响置信度，不改变动作）

意图真值唯一来源是 ``utils/emailbot/intent.py`` 的 LLM 分类器；
关键词表只在本文件维护，意图层通过 ``MailRule.matches()`` 复用，避免两套词表漂移。

匹配域（field）：
- subject   主题
- from      发件人（含显示名与地址）
- body      正文（text，缺失时 parser 已用 html 兜底）
- attachment 附件文件名（简历场景常用）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

# 规则的匹配区域：主题 / 发件人（含显示名与地址）/ 正文 / 附件名
MatchField = Literal["subject", "from", "body", "attachment"]


class RuleAction(str, Enum):
    """规则命中后的动作。"""

    BLOCK = "BLOCK"   # 拦截：直接忽略，不进意图层（省掉一次 LLM 调用）
    ALLOW = "ALLOW"   # 放行：不附任何标签
    PRIOR = "PRIOR"   # 放行 + 打 prior_intent 先验标签（只影响置信度）


@dataclass
class MailRule:
    """L0 确定性预过滤规则：只判「要不要进 LLM」，不判「邮件是什么意思」。

    Args:
        name: 规则名，**仅用于日志/排查**，不参与意图判定
        keywords: 关键词列表，任一命中即算命中；为空则该规则永不命中。
        fields: 在哪些区域里找关键词，可取 ``subject`` / ``from`` / ``body`` /
            ``attachment``，逐区域匹配，任一区域命中即算命中。
        action: 命中后的动作。``BLOCK`` 拦截、不进意图层；``ALLOW`` 放行、不附标签；
            ``PRIOR`` 放行并打 ``prior_intent`` 先验标签。
        prior_intent: 先验意图标签（如 ``"RESUME_SUBMISSION"``），仅在
            ``action=PRIOR`` 时有意义；只影响置信度，不改变动作，**也不等于意图真值**。
        exclude_keywords: 排除词，优先级最高；只要主题/发件人/正文/附件名中出现任一
            排除词，整条规则直接不命中。
        case_sensitive: 是否区分大小写，默认不区分（"CV"/"Resume" 都能命中）。
        require_attachment: 是否必须带附件才命中（"投了简历"场景的硬条件）。
        enabled: 规则总开关，``False`` 时直接跳过，便于灰度或临时下线。
    """

    name: str
    keywords: list[str] = field(default_factory=list)
    fields: list[MatchField] = field(default_factory=lambda: ["subject", "body"])
    action: RuleAction = RuleAction.ALLOW
    prior_intent: str | None = None
    exclude_keywords: list[str] = field(default_factory=list)
    case_sensitive: bool = False
    require_attachment: bool = False
    enabled: bool = True

    def matches(self, subject: str, sender: str, body: str,
                attachment_names: list[str]) -> bool:
        if not self.enabled or not self.keywords:
            return False

        # 排除词优先：命中即不算命中
        whole = f"{subject}\n{sender}\n{body}\n{' '.join(attachment_names)}"
        if self._contains(whole, self.exclude_keywords):
            return False

        if self.require_attachment and not attachment_names:
            return False

        haystack = {
            "subject": subject,
            "from": sender,
            "body": body,
            "attachment": " ".join(attachment_names),
        }
        for f in self.fields:
            text = haystack.get(f, "") or ""
            if not self.case_sensitive:
                text = text.lower()
                words = [k.lower() for k in self.keywords]
            else:
                words = self.keywords
            # 子串包含匹配：够用、快、不会误伤；需要更精细时换成 re.search
            if any(w in text for w in words):
                return True
        return False

    def _contains(self, text: str, words: list[str]) -> bool:
        if not words:
            return False
        low = text.lower()
        return any(w.lower() in low for w in words)

# 使用方法，for循环DEFAULT_RULES，让每个MailRule都matches传入的信息
DEFAULT_RULES = [
    # L0 拦截：自动回复 / 退信 / 系统通知（无需语义理解）
    MailRule(
        name="自动回复与系统通知",
        keywords=[
            "自动回复", "automatic reply", "out of office", "退信",
            "投递失败", "undeliverable", "delivery status notification",
        ],
        action=RuleAction.BLOCK,
    ),
    # L0 拦截：明显营销
    MailRule(
        name="营销邮件",
        keywords=["退订", "unsubscribe", "限时优惠"],
        action=RuleAction.BLOCK,
    ),
    # 强特征：带简历附件的投递 → 先验标签
    MailRule(
        name="简历投递",
        keywords=["简历", "应聘", "求职", "投递", "resume", "cv"],
        fields=["subject", "body", "attachment"],
        require_attachment=True,
        action=RuleAction.PRIOR,
        prior_intent="RESUME_SUBMISSION",
    ),
    # 强特征：面试协商（只用强词；命中后仍低置信 → 转人工，不自动恢复图）
    MailRule(
        name="面试协商",
        keywords=[
            "面试时间", "面试安排", "协商面试", "调整面试", "面试进度",
            "改期", "换个时间", "另约", "重新约", "拒绝面试", "放弃面试",
        ],
        fields=["subject", "body"],
        action=RuleAction.PRIOR,
        prior_intent="INTERVIEW_REPLY",
    ),
]
