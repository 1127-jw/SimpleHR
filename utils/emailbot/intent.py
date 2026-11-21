"""意图识别层：把「规则名路由」升级为「分层意图管道」。

L0 确定性预过滤（rules.py，无 LLM）→ L1 身份识别 → L2 意图分类（LLM + 规则兜底）
→ 产出 ``MailIntentResult`` 交给 ``notify`` 分发。

调用方式与图内节点保持一致：直连模型 + 结构化输出（不使用 Agent），
主模型失败降级备用模型，并带超时、进程内缓存。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from enum import Enum

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableWithFallbacks
from pydantic import BaseModel, Field, field_validator

from app.agents.llms import build_structured_llm
from app.core.config import get_settings
from utils.emailbot.models import ParsedEmail
from utils.emailbot.rules import DEFAULT_RULES, MailRule, RuleAction

logger = logging.getLogger(__name__)
settings = get_settings()


# ==================== 枚举 ====================

class MailIntent(str, Enum):
    RESUME_SUBMISSION = "RESUME_SUBMISSION"   # 新简历投递
    INTERVIEW_REPLY = "INTERVIEW_REPLY"       # 面试协商回复
    PROGRESS_INQUIRY = "PROGRESS_INQUIRY"     # 询问进度/结果
    INTERVIEWER_MAIL = "INTERVIEWER_MAIL"     # 面试官邮件
    AUTO_REPLY = "AUTO_REPLY"                 # 自动回复/OOO/退信/系统通知
    OUT_OF_SCOPE = "OUT_OF_SCOPE"             # 无关邮件
    AMBIGUOUS = "AMBIGUOUS"                   # 低置信，转人工


class SenderRole(str, Enum):
    CANDIDATE = "CANDIDATE"
    INTERVIEWER = "INTERVIEWER"
    HR = "HR"
    SYSTEM = "SYSTEM"
    UNKNOWN = "UNKNOWN"


# ==================== 结构化输出 ====================

class IntentSlots(BaseModel):
    """粗槽位：精细时间解析仍由图内 check_availability 负责。"""
    proposed_time: str = Field(default="", description="候选人期望时间，YYYY-MM-DD HH:MM:SS")
    exactly: str = Field(default="", description="hours / am / pm / all / 空")
    refuse_reason: str = Field(default="", description="拒绝面试的原因")
    position_ref: str = Field(default="", description="提及的职位名，可为空")


class MailIntentResult(BaseModel):
    intent: MailIntent
    sub_intent: str = Field(default="", description="细分，如 confirm / reschedule / refuse")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    slots: IntentSlots = Field(default_factory=IntentSlots)
    evidence: str = Field(default="", description="判定依据原文片段，便于人工复核")
    reason: str = Field(default="", description="分类理由")
    needs_review: bool = Field(default=False, description="低置信或信息不足，需人工复核")

    @field_validator("evidence", mode="after")
    @classmethod
    def _clean_evidence(cls, value: str) -> str:
        """清洗 ``evidence``：模型偶发输出 JSON 碎片（实测出现过 ``":[``）。

        规则：去掉首尾空白 → 太短（<4）或**不含任何文字**（字母/数字/汉字）
        的一律视为碎片置空；否则截断到 200 字。复核页要拿它给人看，
        留一串符号只会误导。
        """
        text = (value or "").strip()
        if len(text) < 4 or not re.search(r"[\w\u4e00-\u9fff]", text):
            return ""
        return text[:200]


# ==================== 提示词 ====================

INTENT_SYSTEM_PROMPT = """你是招聘邮箱的意图分类器。根据【发件人身份】和【流程上下文】判断一封邮件的意图。

# 意图定义
- RESUME_SUBMISSION：投递新简历（通常带简历附件，或正文明确求职/应聘）
- INTERVIEW_REPLY：对面试邀约的回复（确认时间、改期、拒绝、询问时间）
- PROGRESS_INQUIRY：询问招聘进度、结果、后续安排
- INTERVIEWER_MAIL：面试官（非候选人）发来的反馈、改期请求等
- AUTO_REPLY：自动回复、外出提醒、退信、系统通知、订阅邮件
- OUT_OF_SCOPE：与招聘流程无关（营销、错发、骚扰）
- AMBIGUOUS：信息不足或低置信，无法归入以上任何一类

# 硬规则
1. 若「是否在等待候选人回复」为 false，则简短回复（如“好的”“收到”“可以”）不得判为 INTERVIEW_REPLY，
   应判为 OUT_OF_SCOPE 或 AMBIGUOUS。
2. 判定 INTERVIEW_REPLY 必须能对应到具体的面试协商语义，不能仅凭“确认/好的”这类泛词。
3. AUTO_REPLY 优先级最高：命中自动回复特征时直接判定，不再看其他意图。
4. 「规则先验」只是线索，不是结论；与邮件内容冲突时以邮件内容为准。
5. 无法确定时判 AMBIGUOUS 并把 needs_review 设为 true，不要猜。

# 输出
严格输出 JSON：intent、sub_intent、confidence(0-1)、slots、evidence、reason、needs_review。
其中 evidence 必须是【邮件正文】里支撑该判定的**连续原文片段**（≤50 字），
不要输出 JSON 结构、符号或换行；找不到合适的原文就留空字符串。
"""

INTENT_USER_PROMPT = """【发件人身份】{role}
【候选人状态】{candidate_status}
【是否在等待候选人回复】{waiting_reply}
【最近一次我方邮件】{last_outbound_subject}
【是否带附件】{has_attachment}
【规则先验】{prior_intents}

【邮件主题】{subject}

【邮件正文】
{body}
"""


# ==================== 分类器 ====================

_intent_llm: RunnableWithFallbacks | None = None


def _get_intent_llm() -> RunnableWithFallbacks:
    """懒加载带降级链的结构化分类模型（构建逻辑统一在 app.agents.llms）。"""
    global _intent_llm
    if _intent_llm is None:
        _intent_llm = build_structured_llm(MailIntentResult)
    return _intent_llm


def _build_intent_messages(prompt: str) -> list:
    """与图内节点一致的调用方式：SystemMessage + UserMessage 直接 ainvoke。"""
    return [
        SystemMessage(content=INTENT_SYSTEM_PROMPT),
        HumanMessage(content=prompt),
    ]


async def _classify_with_llm(prompt: str) -> MailIntentResult:
    """单次结构化调用，带超时；模型降级由 RunnableWithFallbacks 负责。"""
    from app.core.limiters import llm_slot

    async with llm_slot("llm:intent"):
        return await asyncio.wait_for(
            _get_intent_llm().ainvoke(_build_intent_messages(prompt)),
            timeout=settings.INTENT_LLM_TIMEOUT_SECONDS,
        )


# 进程内缓存：相同邮件内容只分类一次（重复轮询 / 重放时零成本）
_cache: dict[str, MailIntentResult] = {}


def _cache_key(email: ParsedEmail, prior_intents: list[str] | None) -> str:
    raw = f"{email.message_id}|{email.subject}|{email.text[:2000]}|{sorted(prior_intents or [])}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def classify_intent(
    email: ParsedEmail,
    *,
    role: SenderRole = SenderRole.UNKNOWN,
    candidate_status: str = "",
    waiting_reply: bool = False,
    last_outbound_subject: str = "",
    has_attachment: bool = False,
    rules: list[MailRule] | None = None,
    prior_intents: list[str] | None = None,
) -> MailIntentResult:
    """LLM 意图分类；失败或低置信时回落规则兜底。"""
    key = _cache_key(email, prior_intents)
    if key in _cache:
        return _cache[key]

    prompt = INTENT_USER_PROMPT.format(
        role=role.value,
        candidate_status=candidate_status or "未知",
        waiting_reply="是" if waiting_reply else "否",
        last_outbound_subject=last_outbound_subject or "无",
        has_attachment="是" if has_attachment else "否",
        prior_intents="、".join(prior_intents) if prior_intents else "无",
        subject=email.subject,
        body=email.text[: settings.INTENT_MAX_MAIL_CHARS],
    )

    result: MailIntentResult | None = None
    if settings.INTENT_ENGINE in ("llm", "hybrid"):
        try:
            result = await _classify_with_llm(prompt)
        except Exception:
            logger.exception("LLM 意图分类失败，回落规则引擎 | uid=%s", email.uid)

    if result is None:
        result = await _rule_classify(
            email, role=role, waiting_reply=waiting_reply, rules=rules
        )

    if result.confidence < settings.INTENT_CONFIDENCE_THRESHOLD:
        result.needs_review = True

    if len(_cache) >= settings.INTENT_CACHE_SIZE:
        _cache.clear()
    _cache[key] = result
    return result


# ==================== 规则兜底 ====================
# 关键词真相只在 rules.py 的 DEFAULT_RULES 一处，这里复用 MailRule.matches()。

async def _rule_classify(
    email: ParsedEmail,
    *,
    role: SenderRole,
    waiting_reply: bool,
    rules: list[MailRule] | None = None,
) -> MailIntentResult:
    """纯规则兜底：仅在 LLM 不可用时使用。

    只认强特征：L0 拦截（自动回复/退信/营销）、简历附件先验、面试强词先验；
    其余一律返回 AMBIGUOUS 低置信，转人工，绝不猜。
    """
    rules = rules if rules is not None else DEFAULT_RULES

    if any(r.action is RuleAction.BLOCK and _match(r, email) for r in rules):
        return MailIntentResult(
            intent=MailIntent.OUT_OF_SCOPE, confidence=0.9,
            reason="命中 L0 拦截规则（BLOCK）",
        )

    if role is SenderRole.INTERVIEWER:
        return MailIntentResult(
            intent=MailIntent.INTERVIEWER_MAIL, confidence=0.8,
            reason="发件人为面试官",
        )

    for r in rules:
        if r.action is not RuleAction.PRIOR or not _match(r, email):
            continue
        try:
            intent = MailIntent(r.prior_intent) if r.prior_intent else MailIntent.AMBIGUOUS
        except ValueError:
            intent = MailIntent.AMBIGUOUS
        return MailIntentResult(
            intent=intent, confidence=0.6,
            reason=f"命中规则 {r.name}，仅作先验，置信度低",
        )

    return MailIntentResult(
        intent=MailIntent.AMBIGUOUS, confidence=0.3, needs_review=True,
        reason="规则未命中任何强特征，转人工复核",
    )


def _match(rule: MailRule, email: ParsedEmail) -> bool:
    """薄封装：把 ParsedEmail 摊平后交给 MailRule.matches()。"""
    return rule.matches(
        subject=email.subject,
        sender=f"{email.from_.name} {email.from_.address}",
        body=email.text,
        attachment_names=[a.filename for a in email.attachments],
    )
