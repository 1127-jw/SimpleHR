"""候选人回复「同意原时间」必须走确认分支，而不是耗尽协商轮次。

背景：``PROPOSED_TIME_SYSTEM_PROMPT`` 对「同意原时间」要求 ``confirm=True`` 且
``proposed_time`` / ``exactly`` 置空，而 ``check_availability_node`` 里没有
``confirm`` 分支 —— 这条回复会落到「未提供时间」→ ``can_not_find_available_time``
递增轮次，候选人答应得越爽快，越会被判定为协商超时。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pytest

from app.schemas.intent_schema import ProposedTime
from app.workflow.candidate_nodes import check_availability_node as node_mod
from app.workflow.candidate_graph import route_after_check_availability
from app.workflow.candidate_state import REPLY_REFUSED, REPLY_TIME_AVAILABLE
from app.core.time import BEIJING_TZ

# 首次邀请时系统提议的面试时间
PROPOSED = datetime(2026, 11, 20, 10, 30, tzinfo=BEIJING_TZ)


@dataclass
class _Candidate:
    id: str
    name: str
    email: str


@dataclass
class _Interviewer:
    id: str
    email: str


def _state(**overrides) -> dict:
    state = {
        "candidate": _Candidate(id="cand-001", name="张三", email="c@example.com"),
        "position": None,
        "interviewer": _Interviewer(id="user-001", email="hr@example.com"),
        "current_stage": "email_reply",
        "negotiation_round": 0,
        "max_negotiation_rounds": 3,
        "proposed_interview_time": PROPOSED,
        "candidate_email_reply": "可以，就这个时间",
    }
    state.update(overrides)
    return state


def _patch_reply(monkeypatch, **fields) -> None:
    """固定 LLM 对邮件正文的结构化抽取结果。"""

    async def _fake_extract(state, reply):
        return ProposedTime(**fields)

    monkeypatch.setattr(node_mod, "_extract_time_from_reply", _fake_extract)


@pytest.fixture
def dingtalk_calls(monkeypatch) -> list:
    """记录空闲时段查询的调用；同意原时间时应当一次都不发生。"""
    calls: list = []

    async def _fake_slots(*args, **kwargs):
        calls.append(kwargs or args)
        return []

    monkeypatch.setattr(node_mod, "get_user_available_slots", _fake_slots)
    return calls


CONFIRM_REPLY = dict(confirm=True, refused=False, proposed_time="", exactly="")


async def test_confirm_reply_takes_available_branch(monkeypatch, dingtalk_calls):
    _patch_reply(monkeypatch, **CONFIRM_REPLY)

    result = await node_mod.check_availability_node(_state())

    assert result["candidate_email_reply"] == REPLY_TIME_AVAILABLE
    # 不推进轮次：同意原时间不是「协商失败」
    assert "negotiation_round" not in result


async def test_confirm_reply_does_not_requery_dingtalk(monkeypatch, dingtalk_calls):
    """提议时间本就是首次邀请时从空闲时段里挑的，确认阶段无需再查一次。"""
    _patch_reply(monkeypatch, **CONFIRM_REPLY)
    await node_mod.check_availability_node(_state())

    assert dingtalk_calls == []


async def test_confirm_result_routes_to_confirm_node(monkeypatch, dingtalk_calls):
    """节点输出与路由函数的契约：确认分支要落到 confirm，而不是回到等邮件。"""
    _patch_reply(monkeypatch, **CONFIRM_REPLY)

    result = await node_mod.check_availability_node(_state())
    merged = {**_state(), **result}

    assert route_after_check_availability(merged) == "confirmed"
    # 未回写 proposed_interview_time → confirm 节点沿用首次邀约的时间
    assert merged["proposed_interview_time"] == PROPOSED


async def test_refused_wins_when_llm_sets_both_flags(monkeypatch, dingtalk_calls):
    """提示词禁止 confirm/refused 同时为 true；真出现时按拒绝处理，不能凭空安排面试。"""
    _patch_reply(monkeypatch, confirm=True, refused=True, proposed_time="", exactly="")

    result = await node_mod.check_availability_node(_state())

    assert result["candidate_email_reply"] == REPLY_REFUSED
    assert route_after_check_availability({**_state(), **result}) == "refused"


async def test_reply_without_any_signal_still_negotiates(monkeypatch):
    """回归保护：confirm 分支不能写宽，既不同意也没给时间时仍要继续协商。"""
    _patch_reply(monkeypatch, confirm=False, refused=False, proposed_time="", exactly="")

    result = await node_mod.check_availability_node(_state())

    assert result["negotiation_round"] == 1
    assert route_after_check_availability({**_state(), **result}) == "get_candidate_email_reply_node"
