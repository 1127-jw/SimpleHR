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
