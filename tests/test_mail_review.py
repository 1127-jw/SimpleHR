"""意图识别「人工复核」的纯逻辑测试。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models.mail_review import MailReviewModel, MailReviewStatusEnum
from app.service.mail_review_ser import (
    _EXECUTABLE_INTENTS,
    _rebuild_email,
    resolve_review,
)
from utils.emailbot.intent import MailIntent
from utils.emailbot.models import EmailAddress


def _row(**over) -> SimpleNamespace:
    base = dict(
        id="rev-1",
        email_uid="1",
        message_id="<m1@x>",
        from_address="a@qq.com",
        from_name="张三",
        subject="面试时间",
        body_excerpt="正文内容",
        attachment_paths=["/tmp/a.pdf"],
        intent_guess="AMBIGUOUS",
        sub_intent="",
        confidence=0.3,
        reason="信息不足",
        evidence="片段",
        status=MailReviewStatusEnum.PENDING.value,
        resolver_id=None,
        resolved_at=None,
        resolution="",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_rebuild_email_restores_address_and_attachments():
    email = _rebuild_email(_row())
    assert email.from_.address == "a@qq.com"
    assert email.from_.name == "张三"
    assert email.text == "正文内容"
    assert [a.saved_path.name for a in email.attachments] == ["a.pdf"]


def test_executable_intents_cover_the_actionable_ones():
    values = {i.value for i in _EXECUTABLE_INTENTS}
    assert {MailIntent.INTERVIEW_REPLY, MailIntent.RESUME_SUBMISSION} <= values
    assert MailIntent.AMBIGUOUS not in values


class _Repo:
    def __init__(self, session=None):
        pass

    async def get_by_id(self, review_id):
        return _row()

    async def resolve(self, review_id, **kwargs):
        return SimpleNamespace(id=review_id, **kwargs)


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def begin(self):
        return _Tx()


async def test_resolve_rejects_unknown_action(monkeypatch):
    monkeypatch.setattr("app.service.mail_review_ser.MailReviewRepo", _Repo)
    with pytest.raises(ValueError, match="未知的处理动作"):
        await resolve_review(
            _Session(), "rev-1", resolver_id="u1", intent=None, action="whatever"
        )


async def test_resolve_requires_intent_for_execute(monkeypatch):
    monkeypatch.setattr("app.service.mail_review_ser.MailReviewRepo", _Repo)
    with pytest.raises(ValueError, match="必须指定意图"):
        await resolve_review(
            _Session(), "rev-1", resolver_id="u1", intent=None, action="execute"
        )


async def test_resolve_rejects_unknown_intent(monkeypatch):
    monkeypatch.setattr("app.service.mail_review_ser.MailReviewRepo", _Repo)
    with pytest.raises(ValueError, match="未知的意图"):
        await resolve_review(
            _Session(), "rev-1", resolver_id="u1", intent="NOPE", action="execute"
        )


async def test_ignore_does_not_require_intent(monkeypatch):
    """忽略是纯状态变更，不该要求选意图。"""
    captured = {}

    class _Repo2(_Repo):
        async def resolve(self, review_id, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(id=review_id)

    monkeypatch.setattr("app.service.mail_review_ser.MailReviewRepo", _Repo2)
    await resolve_review(
        _Session(), "rev-1", resolver_id="u1", intent=None, action="ignore"
    )
    assert captured["status"] == MailReviewStatusEnum.IGNORED


def test_status_enum_values():
    assert [s.value for s in MailReviewStatusEnum] == ["PENDING", "RESOLVED", "IGNORED"]
