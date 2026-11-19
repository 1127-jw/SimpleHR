"""协商主链路：真正执行节点，而不是只测路由函数。

覆盖三个曾经让流程跑不通的缺陷：

1. ``interrupt()`` 被 ``except Exception`` 吞掉 → 图从不暂停（§十二）；
2. 候选人回复为空时 ``check_availability_node`` 提前返回但不推进轮次
   → 路由原样回到邮件节点反复挂起，协商轮次上限永远碰不到；
3. 调用 ``get_user_available_slots`` 的实参与生产签名不符 → TypeError，
   候选人一旦提出具体时间就落到 PROCESSING_ERROR。
"""
from __future__ import annotations

import inspect
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph
from langgraph.types import Command

from app.schemas.intent_schema import ProposedTime
from app.service import dingtalk_ser
from app.workflow.candidate_nodes import check_availability_node as node_mod
from app.workflow.candidate_nodes import get_candidate_email_reply_node as reply_mod
from app.workflow.candidate_graph import (
    route_after_check_availability,
    route_after_email_reply,
)
from app.workflow.candidate_state import (
    EMAIL_REPLY_TIMEOUT,
    REPLY_TIME_AVAILABLE,
    CandidateRecruitmentState,
)
from app.core.time import BEIJING_TZ

# 与生产一致：钉钉空闲时段与提议时间都是 aware datetime
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
        "current_stage": "email_invite",
        "negotiation_round": 0,
        "max_negotiation_rounds": 2,
    }
    state.update(overrides)
    return state


def _free_slots():
    """一整段空闲：10:00 - 11:00，能容纳 10:30 起的 30 分钟面试。"""
    return [(PROPOSED.replace(minute=0), PROPOSED.replace(minute=0) + timedelta(hours=1))]


@pytest.fixture
def patched_llm_time(monkeypatch):
    """把 LLM 抽取结果固定成「提出 PROPOSED 这个准确时间」。"""

    async def _fake_extract(state, reply):
        return ProposedTime(
            confirm=False,
            refused=False,
            proposed_time=PROPOSED.strftime("%Y-%m-%d %H:%M:%S"),
            exactly="hours",
        )

    monkeypatch.setattr(node_mod, "_extract_time_from_reply", _fake_extract)


# ==================== 缺陷 3：调用签名 ====================

async def test_available_slots_call_matches_production_signature(
    monkeypatch, patched_llm_time
):
    """节点传给钉钉服务的实参必须能被生产签名接受。

    用 ``**kwargs`` 收集实参、再用 ``Signature.bind`` 对照真实函数，
    这样任一侧改签名都会在这里暴露，而不是等线上抛 TypeError。
    """
    captured: list[dict] = []

    async def _fake_slots(**kwargs):
        captured.append(kwargs)
        return _free_slots()

    monkeypatch.setattr(node_mod, "get_user_available_slots", _fake_slots)

    result = await node_mod.check_availability_node(
        _state(candidate_email_reply="20号上午10点半可以吗")
    )

    assert captured, "节点没有调用 get_user_available_slots"
    inspect.signature(dingtalk_ser.get_user_available_slots).bind(**captured[0])

    assert result["candidate_email_reply"] == REPLY_TIME_AVAILABLE
    assert result["proposed_interview_time"] == PROPOSED


async def test_available_slots_search_starts_on_proposed_day(
    monkeypatch, patched_llm_time
):
    """查询窗口应从提议日 00:00 起、只覆盖当天，避免漏掉当天更早的忙碌日程。"""
    captured: list[dict] = []

    async def _fake_slots(**kwargs):
        captured.append(kwargs)
        return _free_slots()

    monkeypatch.setattr(node_mod, "get_user_available_slots", _fake_slots)
    await node_mod.check_availability_node(_state(candidate_email_reply="10点半"))

    call = captured[0]
    assert call["interviewer_id"] == "user-001"
    assert call["start_time"] == PROPOSED.replace(hour=0, minute=0, second=0, microsecond=0)
    assert call["max_days_to_search"] == 1
    # 切分粒度等于面试时长，否则 10:45 这类提议会被误判为不可用
    assert call["hour_interval"] == pytest.approx(
        node_mod.INTERVIEW_DURATION.total_seconds() / 3600
    )


async def test_busy_slot_makes_proposal_unavailable(monkeypatch):
    """空闲列表与提议时间不匹配时，必须走「再协商」并递增轮次。"""

    async def _fake_extract(state, reply):
        return ProposedTime(
            confirm=False, refused=False,
            proposed_time=PROPOSED.strftime("%Y-%m-%d %H:%M:%S"), exactly="hours",
        )

    async def _fake_slots(**kwargs):
        return [(PROPOSED.replace(hour=14), PROPOSED.replace(hour=14) + timedelta(minutes=30))]

    monkeypatch.setattr(node_mod, "_extract_time_from_reply", _fake_extract)
    monkeypatch.setattr(node_mod, "get_user_available_slots", _fake_slots)

    result = await node_mod.check_availability_node(_state(candidate_email_reply="10点半"))

    assert "candidate_email_reply" not in result
    assert result["negotiation_round"] == 1


async def test_am_pm_window_compare_is_timezone_safe():
    """「上午/下午」这类模糊提议走 check_am_or_pm，窗口与空闲时段必须同时区。

    修复前 ``datetime.combine`` 产出 naive，与钉钉返回的 aware 时段比较直接抛
    TypeError，候选人只要回复「明天上午」流程就失败。
    """
    aware_slots = [
        (datetime(2026, 11, 20, 9, 0, tzinfo=BEIJING_TZ),
         datetime(2026, 11, 20, 10, 0, tzinfo=BEIJING_TZ))
    ]
    proposed_date = datetime(2026, 11, 20, 0, 0, tzinfo=BEIJING_TZ)

    assert node_mod.check_am_or_pm(
        aware_slots, proposed_date, timedelta(minutes=30), node_mod.DAILY_WINDOWS["am"]
    )
    assert not node_mod.check_am_or_pm(
        aware_slots, proposed_date, timedelta(hours=2), node_mod.DAILY_WINDOWS["am"]
    )


# ==================== 缺陷 2：空回复不再死循环 ====================

async def test_empty_reply_advances_negotiation_round():
    """空回复也必须推进轮次，否则路由会原地回到邮件节点反复循环。"""
    result = await node_mod.check_availability_node(_state(candidate_email_reply=""))

    assert result["negotiation_round"] == 1
    assert route_after_check_availability(
        _state(candidate_email_reply="", negotiation_round=1)
    ) == "get_candidate_email_reply_node"
    assert route_after_check_availability(
        _state(candidate_email_reply="", negotiation_round=2)
    ) == "negotiation_expired"


# ==================== 缺陷 1：整图真的会挂起并收敛 ====================

def _build_negotiation_graph(on_expired: list):
    """只挂协商环节的两个真实节点，收尾节点用替身（避免触碰数据库）。"""

    async def negotiation_expired_stub(state):
        on_expired.append(state.get("negotiation_round", 0))
        return {"current_stage": "negotiation_expired"}

    async def confirm_stub(state):
        on_expired.append("confirmed")
        return {"current_stage": "confirmed"}

    builder = StateGraph(CandidateRecruitmentState)
    builder.add_node("get_candidate_email_reply_node", reply_mod.get_candidate_email_reply_node)
    builder.add_node("check_availability", node_mod.check_availability_node)
    builder.add_node("negotiation_expired", negotiation_expired_stub)
    builder.add_node("confirm", confirm_stub)

    builder.add_edge(START, "get_candidate_email_reply_node")
    builder.add_conditional_edges(
        "get_candidate_email_reply_node",
        route_after_email_reply,
        {
            "check_availability": "check_availability",
            "negotiation_expired": "negotiation_expired",
            "error": "negotiation_expired",
        },
    )
    builder.add_conditional_edges(
        "check_availability",
        route_after_check_availability,
        {
            "confirmed": "confirm",
            "refused": "negotiation_expired",
            "get_candidate_email_reply_node": "get_candidate_email_reply_node",
            "negotiation_expired": "negotiation_expired",
            "error": "negotiation_expired",
        },
    )
    return builder.compile(checkpointer=InMemorySaver())


async def test_graph_pauses_then_resumes_and_completes(monkeypatch, patched_llm_time):
    """挂起 → 恢复给出时间 → 校验通过 → 走到确认节点。"""

    async def _fake_slots(**kwargs):
        return _free_slots()

    monkeypatch.setattr(node_mod, "get_user_available_slots", _fake_slots)

    expired: list = []
    graph = _build_negotiation_graph(expired)
    config = {"configurable": {"thread_id": uuid.uuid4().hex}}

    first = [chunk async for chunk in graph.astream(_state(), config)]
    assert any("__interrupt__" in chunk for chunk in first), "图必须在等待邮件回复处挂起"

    final = [
        chunk
        async for chunk in graph.astream(Command(resume="20号上午10点半"), config)
    ]
    assert expired == ["confirmed"], f"应走到确认节点，实际收尾={expired}"
    assert final, "恢复后图应当有输出"


async def test_repeated_empty_replies_terminate_at_round_cap(monkeypatch):
    """每轮都回空内容：轮次递增，到达上限后收敛到协商超时，不得无限循环。"""
    expired: list = []
    graph = _build_negotiation_graph(expired)
    config = {
        "configurable": {"thread_id": uuid.uuid4().hex},
        "recursion_limit": 40,
    }

    async for _ in graph.astream(_state(), config):
        pass

    # 2 轮空回复后应当结束（max_negotiation_rounds=2）
    for _ in range(2):
        chunks = [c async for c in graph.astream(Command(resume=""), config)]
        assert chunks is not None

    assert expired == [2], f"应在第 2 轮后超时收敛，实际={expired}"
    snapshot = graph.get_state(config)
    assert snapshot.next == (), "流程应已结束，不该仍停在待执行节点"


async def test_timeout_resume_routes_to_expired_without_llm(monkeypatch):
    """巡检注入超时标记后直接结束，不再调用 LLM 解析空邮件。"""
    expired: list = []
    graph = _build_negotiation_graph(expired)
    config = {"configurable": {"thread_id": uuid.uuid4().hex}}

    async for _ in graph.astream(_state(), config):
        pass
    [c async for c in graph.astream(Command(resume=EMAIL_REPLY_TIMEOUT), config)]

    assert expired == [0]
