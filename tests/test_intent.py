"""意图识别层单元测试（纯内存，不依赖 DB / Redis / 网络 / LLM）。"""
from __future__ import annotations

import asyncio

from utils.emailbot import intent as intent_module
from utils.emailbot.intent import MailIntent, SenderRole, classify_intent
from utils.emailbot.models import EmailAddress, ParsedEmail
from utils.emailbot.rules import DEFAULT_RULES, RuleAction


def _mail(subject: str, text: str, attachments: list[str] | None = None) -> ParsedEmail:
    return ParsedEmail(
        uid="1",
        message_id="<t@local>",
        subject=subject,
        from_=EmailAddress(name="张三", address="z@ex.com"),
        text=text,
        attachments=attachments or [],
    )


def _rule(name: str):
    return next(r for r in DEFAULT_RULES if r.name == name)


# ==================== L0 规则 ====================

def test_l0_block_rule_hits_auto_reply():
    """自动回复命中 BLOCK 规则：廉价拦截，不进 LLM。"""
    rule = _rule("自动回复与系统通知")
    assert rule.action is RuleAction.BLOCK
    assert rule.matches("自动回复", "z@ex.com", "外出办公，稍后回复", [])


def test_generic_words_no_longer_hit_any_rule():
    """"好的""确认"这类泛词不再命中任何规则（误报根源已下线）。"""
    mail = _mail("Re: 面试邀请", "好的，确认")
    hit = [
        r for r in DEFAULT_RULES
        if r.matches(mail.subject, "张三 z@ex.com", mail.text, [])
    ]
    assert hit == []


def test_interview_strong_words_hit_prior():
    """面试协商强词命中 PRIOR，且先验标签正确。"""
    rule = _rule("面试协商")
    assert rule.action is RuleAction.PRIOR
    assert rule.prior_intent == "INTERVIEW_REPLY"
    assert rule.matches("Re: 面试邀请", "张三 z@ex.com", "面试时间想改期", [])


def test_resume_prior_rule_requires_attachment():
    """简历投递规则必须带附件才命中。"""
    rule = _rule("简历投递")
    assert rule.action is RuleAction.PRIOR
    assert rule.prior_intent == "RESUME_SUBMISSION"
    assert not rule.matches("应聘 Java", "张三 z@ex.com", "请查收简历", [])
    assert rule.matches("应聘 Java", "张三 z@ex.com", "请查收简历", ["resume.pdf"])


def test_exclude_keywords_take_priority():
    """排除词优先于关键词。"""
    from utils.emailbot.rules import MailRule

    rule = MailRule(
        name="t", keywords=["简历"], exclude_keywords=["unsubscribe"],
        action=RuleAction.PRIOR, prior_intent="RESUME_SUBMISSION",
    )
    assert not rule.matches("简历", "z@ex.com", "unsubscribe 退订", [])


# ==================== 规则兜底 ====================

def test_rule_fallback_marks_generic_as_review(monkeypatch):
    """规则兜底下，"好的"判为 AMBIGUOUS 且转人工，绝不猜。"""
    intent_module._cache.clear()
    monkeypatch.setattr(intent_module.settings, "INTENT_ENGINE", "rule")
    r = asyncio.run(classify_intent(
        _mail("Re: 面试邀请", "好的"),
        role=SenderRole.CANDIDATE, waiting_reply=False,
    ))
    assert r.intent is MailIntent.AMBIGUOUS
    assert r.needs_review is True


def test_rule_fallback_block_is_out_of_scope(monkeypatch):
    """BLOCK 规则兜底判为 OUT_OF_SCOPE（高置信，不转人工）。"""
    intent_module._cache.clear()
    monkeypatch.setattr(intent_module.settings, "INTENT_ENGINE", "rule")
    r = asyncio.run(classify_intent(
        _mail("自动回复", "外出办公，稍后回复"),
        role=SenderRole.CANDIDATE, waiting_reply=True,
    ))
    assert r.intent is MailIntent.OUT_OF_SCOPE
    assert r.needs_review is False


def test_rule_fallback_prior_gives_low_confidence(monkeypatch):
    """命中 PRIOR 强词 → 低置信 + 转人工（不自动恢复图）。"""
    intent_module._cache.clear()
    monkeypatch.setattr(intent_module.settings, "INTENT_ENGINE", "rule")
    r = asyncio.run(classify_intent(
        _mail("Re: 面试邀请", "面试时间想改期"),
        role=SenderRole.CANDIDATE, waiting_reply=True,
    ))
    assert r.intent is MailIntent.INTERVIEW_REPLY
    assert r.needs_review is True


def test_cache_returns_same_result(monkeypatch):
    """相同邮件 + 相同先验 → 命中缓存。"""
    intent_module._cache.clear()
    monkeypatch.setattr(intent_module.settings, "INTENT_ENGINE", "rule")
    mail = _mail("自动回复", "外出办公")
    r1 = asyncio.run(classify_intent(mail, role=SenderRole.CANDIDATE))
    r2 = asyncio.run(classify_intent(mail, role=SenderRole.CANDIDATE))
    assert r1 is r2


# ---------------- evidence 清洗（模型偶发输出 JSON 碎片） ----------------

def test_evidence_drops_json_fragments():
    """实测出现过 evidence=":[" —— 模型把 JSON 结构碎片当成了片段。"""
    from utils.emailbot.intent import MailIntent, MailIntentResult

    assert MailIntentResult(intent=MailIntent.AMBIGUOUS, evidence=":[").evidence == ""
    assert MailIntentResult(intent=MailIntent.AMBIGUOUS, evidence="{...}").evidence == ""
    assert MailIntentResult(intent=MailIntent.AMBIGUOUS, evidence="   ").evidence == ""


def test_evidence_keeps_real_quote_and_caps_length():
    from utils.emailbot.intent import MailIntent, MailIntentResult

    r = MailIntentResult(intent=MailIntent.INTERVIEW_REPLY, evidence="候选人提出下午2点")
    assert r.evidence == "候选人提出下午2点"

    long = MailIntentResult(intent=MailIntent.INTERVIEW_REPLY, evidence="甲" * 500)
    assert len(long.evidence) == 200


# ---------------- 「待面试」状态：走不通的恢复路径不显示为面试回复 ----------------

def test_waiting_interview_forces_review_even_when_rule_says_reply(monkeypatch):
    """规则兜底判成 INTERVIEW_REPLY，但候选人已「待面试」→ 强制转人工。

    系统当前不支持「待面试 → 重新协商」（已规划），
    与其让 notify 走一条走不通的恢复路径，不如在意图层明确转人工。
    """
    from app.models.candidate import CandidateStatusEnum

    intent_module._cache.clear()
    monkeypatch.setattr(intent_module.settings, "INTENT_ENGINE", "rule")
    r = asyncio.run(classify_intent(
        _mail("Re: 面试邀请", "面试时间想改到下周"),
        role=SenderRole.CANDIDATE,
        waiting_reply=True,
        candidate_status=CandidateStatusEnum.WAITING_FOR_INTERVIEW.value,
    ))
    assert r.intent is MailIntent.AMBIGUOUS
    assert r.needs_review is True


def test_waiting_interview_downgrades_llm_reply(monkeypatch):
    """LLM 明确判 INTERVIEW_REPLY，但候选人已「待面试」→ 同样降级为转人工。"""
    from app.models.candidate import CandidateStatusEnum
    from utils.emailbot.intent import MailIntent, MailIntentResult

    intent_module._cache.clear()
    monkeypatch.setattr(intent_module.settings, "INTENT_ENGINE", "llm")

    async def _fake_llm(_messages):
        return MailIntentResult(
            intent=MailIntent.INTERVIEW_REPLY,
            sub_intent="reschedule",
            confidence=0.95,
            evidence="想改到下周",
            reason="候选人提出改期",
        )

    monkeypatch.setattr(intent_module, "_classify_with_llm", _fake_llm)

    r = asyncio.run(classify_intent(
        _mail("Re: 面试邀请", "面试时间想改到下周"),
        role=SenderRole.CANDIDATE,
        waiting_reply=True,
        candidate_status=CandidateStatusEnum.WAITING_FOR_INTERVIEW.value,
    ))
    assert r.intent is MailIntent.AMBIGUOUS
    assert r.needs_review is True
    assert "待面试" in r.reason


def test_negotiating_status_keeps_reply_intent(monkeypatch):
    """对照组：状态为「面试时间协商中」时，改期回复仍应判 INTERVIEW_REPLY。"""
    from app.models.candidate import CandidateStatusEnum
    from utils.emailbot.intent import MailIntent, MailIntentResult

    intent_module._cache.clear()
    monkeypatch.setattr(intent_module.settings, "INTENT_ENGINE", "llm")

    async def _fake_llm(_messages):
        return MailIntentResult(
            intent=MailIntent.INTERVIEW_REPLY,
            sub_intent="reschedule",
            confidence=0.95,
            evidence="想改到下周",
            reason="候选人提出改期",
        )

    monkeypatch.setattr(intent_module, "_classify_with_llm", _fake_llm)

    r = asyncio.run(classify_intent(
        _mail("Re: 面试邀请", "面试时间想改到下周"),
        role=SenderRole.CANDIDATE,
        waiting_reply=True,
        candidate_status=CandidateStatusEnum.NEGOTIATING.value,
    ))
    assert r.intent is MailIntent.INTERVIEW_REPLY
    assert r.needs_review is False
