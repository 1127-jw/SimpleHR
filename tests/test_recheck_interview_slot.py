"""复核节点测试：确认时间后、创建日程前，再查一次面试官该时段是否仍空闲。

背景：协商可能持续好几天，期间面试官完全可能在该时段排了别的日程。
若不复核就建日程，会出现「日程与面试官已有安排撞车」。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from langgraph.checkpoint.memory import InMemorySaver

from app.core.exceptions import EmailSendError
from app.core.time import BEIJING_TZ, now_beijing
from app.workflow import candidate_graph as graph_mod
from app.workflow.candidate_nodes import recheck_interview_slot_node as recheck_mod
from app.workflow.candidate_state import (
    REPLY_SLOT_TAKEN,
    REPLY_TIME_AVAILABLE,
)

# 动态取「48 小时后」：写死日期的话，测试跑过那天就会因为
# 「时段已过去」的新校验而集体变红。
PROPOSED = now_beijing() + timedelta(hours=48)


@dataclass
class _Candidate:
    id: str = "cand-001"
    name: str = "张三"
    email: str = "c@example.com"


@dataclass
class _Interviewer:
    id: str = "user-001"


@dataclass
class _Position:
    title: str = "后端工程师"


def _state(**overrides) -> dict:
    state = {
        "candidate": _Candidate(),
        "position": _Position(),
        "interviewer": _Interviewer(),
        "current_stage": "check_availability",
        "negotiation_round": 2,
        "proposed_interview_time": PROPOSED,
    }
    state.update(overrides)
    return state


async def _slots_free(**kwargs):
    """从 PROPOSED 整点起 2 小时空闲，能容纳 PROPOSED 起的 30 分钟面试。

    注意：PROPOSED 是 ``now + 48h``，分钟/秒是随机的；不能用
    ``PROPOSED.replace(minute=0)`` 当起点（只清分钟、保留秒，会得到
    ``12:00:35`` 这种起点，导致 ``PROPOSED + 30min`` 超出 1 小时窗口）。
    """
    start = PROPOSED.replace(minute=0, second=0, microsecond=0)
    return [(start, start + timedelta(hours=2))]


async def _slots_busy(**kwargs):
    """该时段已被别的日程占掉，只剩下午有空。"""
    return [(PROPOSED.replace(hour=14), PROPOSED.replace(hour=14) + timedelta(hours=1))]


class _RecordingBot:
    is_connected = True

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def connect(self) -> None:
        pass

    async def send_email(self, **kwargs) -> None:
        self.sent.append(kwargs)


class _FailingBot:
    is_connected = True

    async def connect(self) -> None:
        pass

    async def send_email(self, **kwargs) -> None:
        raise EmailSendError("SMTP 挂了")


async def test_free_slot_continues_to_confirm(monkeypatch):
    bot = _RecordingBot()
    monkeypatch.setattr(recheck_mod, "get_user_available_slots", _slots_free)
    monkeypatch.setattr("utils.emailbot.mail_manager.get_mail_bot_", lambda: bot)

    result = await recheck_mod.recheck_interview_slot_node(_state())

    assert result["candidate_email_reply"] == REPLY_TIME_AVAILABLE
    assert bot.sent == [], "时段仍空闲时不需要发信"
    merged = {**_state(), **result}
    assert graph_mod.route_after_recheck_interview_slot(merged) == "confirm"


async def test_taken_slot_resets_round_and_renotifies(monkeypatch):
    """被占用：轮次归零 + 重新发约时间的邮件 + 路由回等待回复。"""
    bot = _RecordingBot()
    monkeypatch.setattr(recheck_mod, "get_user_available_slots", _slots_busy)
    monkeypatch.setattr("utils.emailbot.mail_manager.get_mail_bot_", lambda: bot)

    result = await recheck_mod.recheck_interview_slot_node(_state(negotiation_round=2))

    assert result["candidate_email_reply"] == REPLY_SLOT_TAKEN
    assert result["negotiation_round"] == 0, "必须归零，否则重新协商没有完整轮次"
    assert len(bot.sent) == 1
    assert PROPOSED.strftime("%Y-%m-%d %H:%M") in bot.sent[0]["text"]

    merged = {**_state(), **result}
    assert (
        graph_mod.route_after_recheck_interview_slot(merged)
        == "get_candidate_email_reply_node"
    )


async def test_missing_proposed_time_is_error_state():
    """上游状态错乱（没有最终时间）时要进 error，而不是裸 KeyError。"""
    state = _state()
    state.pop("proposed_interview_time", None)

    result = await recheck_mod.recheck_interview_slot_node(state)

    assert result["error_occurred"] is True


async def test_notify_failure_goes_to_error(monkeypatch):
    """重发协商邮件是「重启协商」的手段，发不出去流程就卡死——必须报错。"""
    monkeypatch.setattr(recheck_mod, "get_user_available_slots", _slots_busy)
    monkeypatch.setattr("utils.emailbot.mail_manager.get_mail_bot_", lambda: _FailingBot())

    result = await recheck_mod.recheck_interview_slot_node(_state())

    assert result["error_occurred"] is True


def test_production_graph_wires_recheck():
    """复核必须真的接在生产图上，且不允许绕过它直接建日程。"""
    compiled = graph_mod.build_candidate_graph(checkpointer=InMemorySaver())

    check_edges = [e for e in compiled.get_graph().edges if e.source == "check_availability"]
    targets = {e.target for e in check_edges}
    assert "recheck_interview_slot" in targets, "「确认时间」之后必须先复核"
    assert "confirm" not in targets, "不允许绕过复核直接建日程"

    recheck_edges = [e for e in compiled.get_graph().edges if e.source == "recheck_interview_slot"]
    assert {e.target for e in recheck_edges} == {
        "confirm",
        "get_candidate_email_reply_node",
        "error",
    }
    assert all(e.conditional for e in recheck_edges), "出边必须是条件边，错误守卫才生效"


async def test_past_slot_is_treated_as_unavailable(monkeypatch):
    """时段已经过去：不必再查钉钉，直接按「不可用」重启协商。

    协商可能拖过周末/假期，候选人确认的可能是个已经过去的时间。
    """
    bot = _RecordingBot()

    def _should_not_be_called(*a, **k):
        raise AssertionError("时段已过去时不应再调用钉钉")

    monkeypatch.setattr(recheck_mod, "get_user_available_slots", _should_not_be_called)
    monkeypatch.setattr("utils.emailbot.mail_manager.get_mail_bot_", lambda: bot)

    past = now_beijing() - timedelta(hours=2)
    result = await recheck_mod.recheck_interview_slot_node(
        _state(proposed_interview_time=past, negotiation_round=1)
    )

    assert result["candidate_email_reply"] == REPLY_SLOT_TAKEN
    assert result["negotiation_round"] == 0
    assert len(bot.sent) == 1
    assert past.strftime("%Y-%m-%d %H:%M") in bot.sent[0]["text"]
