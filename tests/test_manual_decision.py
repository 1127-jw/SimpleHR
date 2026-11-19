"""人工闸门测试：评分通过后必须停下来等 HR 决定，点「发送邮件」才继续。

背景：原流程 ``scoring → email_invite`` 自动发邀请。现在中间插一道
``manual_decision`` 闸门（interrupt），由前端操作唤醒。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph
from langgraph.types import Command, Interrupt

from app.core.exceptions import StatusFlowError
from app.models.candidate import CandidateStatusEnum
from app.service import candidate_status_ser
from app.workflow import candidate_graph as graph_mod
from app.workflow.candidate_graph import route_after_manual_decision
from app.workflow.candidate_nodes import manual_decision_node as md_mod
from app.workflow.candidate_state import (
    EMAIL_REPLY_INTERRUPT,
    MANUAL_DECISION_APPROVE,
    MANUAL_DECISION_INTERRUPT,
    MANUAL_DECISION_REJECT,
    CandidateRecruitmentState,
    is_waiting_on,
)


@dataclass
class _Candidate:
    id: str = "cand-001"
    name: str = "张三"
    email: str = "c@example.com"


@dataclass
class _Position:
    id: str = "pos-001"
    title: str = "后端工程师"


@dataclass
class _Interviewer:
    id: str = "user-001"


def _state(**overrides) -> dict:
    state = {
        "candidate": _Candidate(),
        "position": _Position(),
        "interviewer": _Interviewer(),
        "current_stage": "scoring",
    }
    state.update(overrides)
    return state


# ---------------- 路由 ----------------

def test_route_approve_goes_to_email_invite():
    assert route_after_manual_decision(_state(manual_decision=MANUAL_DECISION_APPROVE)) == "email_invite"


def test_route_reject_goes_to_rejected():
    assert route_after_manual_decision(_state(manual_decision=MANUAL_DECISION_REJECT)) == "rejected"


def test_route_unknown_decision_is_rejected():
    """没写 manual_decision（异常情况）时按淘汰处理，不能凭空发邮件。"""
    assert route_after_manual_decision(_state()) == "rejected"


# ---------------- 生产图接线 ----------------

def test_production_graph_wires_manual_gate():
    compiled = graph_mod.build_candidate_graph(checkpointer=InMemorySaver())

    scoring_targets = {
        e.target for e in compiled.get_graph().edges if e.source == "scoring"
    }
    # email_invite：skip_scoring=True 时直达邀请（见 §3.4）
    assert scoring_targets == {"manual_decision", "email_invite", "rejected", "error"}

    gate_targets = {
        e.target for e in compiled.get_graph().edges if e.source == "manual_decision"
    }
    assert gate_targets == {"email_invite", "rejected", "archive", "error"}
    assert all(
        e.conditional
        for e in compiled.get_graph().edges
        if e.source == "manual_decision"
    )


def test_terminal_nodes_are_wired_through_archive_and_settle():
    """所有终态都必须经过收尾节点——否则链表里不会有终止记录。

    ``confirm`` → ``settle``（结算，保留「待面试」）；
    其余终态 → ``archive``（写 archived_at）。
    """
    compiled = graph_mod.build_candidate_graph(checkpointer=InMemorySaver())
    edges = compiled.get_graph().edges

    assert {e.target for e in edges if e.source == "confirm"} == {"settle"}
    for node in ("rejected", "refused", "negotiation_expired", "error"):
        assert {e.target for e in edges if e.source == node} == {"archive"}

    direct_end = {e.source for e in edges if e.target == "__end__"}
    assert direct_end == {"archive", "settle"}, "只有收尾节点能直连 END"


# ---------------- 图层面的挂起 / 唤醒 ----------------

def _build_gate_graph(reached: list, set_status) -> object:
    async def invite_stub(state):
        reached.append("email_invite")
        return {"current_stage": "email_invite"}

    async def rejected_stub(state):
        reached.append("rejected")
        return {"current_stage": "rejected"}

    builder = StateGraph(CandidateRecruitmentState)
    builder.add_node("manual_decision", md_mod.manual_decision_node)
    builder.add_node("email_invite", invite_stub)
    builder.add_node("rejected", rejected_stub)
    builder.add_edge(START, "manual_decision")
    builder.add_conditional_edges(
        "manual_decision",
        graph_mod._with_error_check(route_after_manual_decision),
        {"email_invite": "email_invite", "rejected": "rejected"},
    )
    return builder.compile(checkpointer=InMemorySaver())


async def test_gate_pauses_then_approves(monkeypatch):
    """评分通过后图必须挂在闸门；resume approve 才去发邀请。"""
    status_calls: list = []

    async def fake_set_status(cid, status):
        status_calls.append((cid, status))

    monkeypatch.setattr(md_mod, "_set_status", fake_set_status)

    reached: list = []
    graph = _build_gate_graph(reached, fake_set_status)
    config = {"configurable": {"thread_id": uuid.uuid4().hex}}

    first = [c async for c in graph.astream(_state(), config)]
    assert any("__interrupt__" in c for c in first), "闸门必须让图挂起"
    assert reached == [], "挂起时还不该发任何邮件"

    await graph.ainvoke(Command(resume=MANUAL_DECISION_APPROVE), config)

    assert reached == ["email_invite"]
    assert status_calls[0][1].value == "待人工确认", "挂起前要把状态置为待人工确认"
    assert status_calls[-1][1].value == "面试时间协商中", "确认后直接进入协商"


async def test_gate_reject_skips_invite(monkeypatch):
    status_calls: list = []

    async def fake_set_status(cid, status):
        status_calls.append((cid, status))

    monkeypatch.setattr(md_mod, "_set_status", fake_set_status)

    reached: list = []
    graph = _build_gate_graph(reached, fake_set_status)
    config = {"configurable": {"thread_id": uuid.uuid4().hex}}

    [c async for c in graph.astream(_state(), config)]
    await graph.ainvoke(Command(resume=MANUAL_DECISION_REJECT), config)

    assert reached == ["rejected"], "拒绝后直接淘汰，绝不能发邀请邮件"
    # resume 会从头重跑闸门节点，所以「待人工确认」会写两次（同一个值，幂等）
    assert [s.value for _, s in status_calls] == ["待人工确认", "待人工确认"]


# ---------------- 服务层：防误 resume ----------------

class _Snapshot:
    def __init__(self, interrupts):
        self.interrupts = interrupts


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def begin(self):
        return _Tx()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _StubGraph:
    def __init__(self, interrupts):
        self._interrupts = interrupts
        self.resumed = []

    async def aget_state(self, config):
        return _Snapshot(self._interrupts)

    async def ainvoke(self, cmd, config):
        self.resumed.append(cmd)
        return None


async def test_resume_requires_pending_state(monkeypatch):
    """不在待人工确认状态时不能 resume，否则会对着已结束的 thread 乱发。"""
    monkeypatch.setattr(
        candidate_status_ser, "_load_graph_models", _fake_load,
    )
    monkeypatch.setattr(candidate_status_ser, "get_checkpointer", lambda *a, **k: object())
    monkeypatch.setattr(
        candidate_status_ser, "build_candidate_graph", lambda *a, **k: _StubGraph(interrupts=[]),
    )

    with pytest.raises(StatusFlowError, match="不在待人工确认状态"):
        await candidate_status_ser.resume_manual_decision("cand-001", MANUAL_DECISION_APPROVE)


async def test_resume_passes_decision_through(monkeypatch):
    monkeypatch.setattr(
        candidate_status_ser, "_load_graph_models", _fake_load,
    )
    monkeypatch.setattr(candidate_status_ser, "get_checkpointer", lambda *a, **k: object())
    stub = _StubGraph(interrupts=[Interrupt(value={"node": MANUAL_DECISION_INTERRUPT})])
    monkeypatch.setattr(candidate_status_ser, "build_candidate_graph", lambda *a, **k: stub)

    await candidate_status_ser.resume_manual_decision("cand-001", MANUAL_DECISION_REJECT)

    assert len(stub.resumed) == 1
    assert getattr(stub.resumed[0], "resume", None) == MANUAL_DECISION_REJECT


async def test_resume_rejects_other_interrupt(monkeypatch):
    """挂起的是别的中断点（如等待候选人回复）时，不能用闸门的决定去 resume。

    实测事故：闸门挂起期间被超时巡检用 "TIMEOUT" 唤醒，候选人直接被判淘汰。
    """
    monkeypatch.setattr(
        candidate_status_ser, "_load_graph_models", _fake_load,
    )
    monkeypatch.setattr(candidate_status_ser, "get_checkpointer", lambda *a, **k: object())
    stub = _StubGraph(interrupts=[Interrupt(value=EMAIL_REPLY_INTERRUPT)])
    monkeypatch.setattr(candidate_status_ser, "build_candidate_graph", lambda *a, **k: stub)

    with pytest.raises(StatusFlowError, match="不在待人工确认状态"):
        await candidate_status_ser.resume_manual_decision("cand-001", MANUAL_DECISION_APPROVE)


async def test_resume_rejects_when_db_status_not_pending(monkeypatch):
    """检查点在闸门、但库里状态不是「待人工确认」——以库为准，拒绝 resume。"""
    monkeypatch.setattr(
        candidate_status_ser,
        "_load_graph_models",
        _fake_load_with_status(CandidateStatusEnum.AI_FILTER_PASSED),
    )
    monkeypatch.setattr(candidate_status_ser, "get_checkpointer", lambda *a, **k: object())
    stub = _StubGraph(interrupts=[Interrupt(value={"node": MANUAL_DECISION_INTERRUPT})])
    monkeypatch.setattr(candidate_status_ser, "build_candidate_graph", lambda *a, **k: stub)

    with pytest.raises(StatusFlowError, match="不是待人工确认"):
        await candidate_status_ser.resume_manual_decision("cand-001", MANUAL_DECISION_APPROVE)


async def _fake_load(cid, status=CandidateStatusEnum.PENDING_MANUAL_DECISION):
    return _models_with_status(status)


def _fake_load_with_status(status):
    async def _load(cid):
        return _models_with_status(status)

    return _load


# ---------------- 中断点身份识别（本次事故的核心） ----------------

def test_is_waiting_on_matches_string_marker():
    assert is_waiting_on([Interrupt(value=EMAIL_REPLY_INTERRUPT)], EMAIL_REPLY_INTERRUPT)


def test_is_waiting_on_matches_dict_marker():
    assert is_waiting_on(
        [Interrupt(value={"node": MANUAL_DECISION_INTERRUPT})], MANUAL_DECISION_INTERRUPT
    )


def test_is_waiting_on_rejects_other_marker():
    """闸门挂起时，不能把它当成「等待候选人回复」。"""
    assert not is_waiting_on(
        [Interrupt(value={"node": MANUAL_DECISION_INTERRUPT})], EMAIL_REPLY_INTERRUPT
    )
    assert not is_waiting_on([Interrupt(value=EMAIL_REPLY_INTERRUPT)], MANUAL_DECISION_INTERRUPT)
    assert not is_waiting_on([], EMAIL_REPLY_INTERRUPT)
    assert not is_waiting_on(None, MANUAL_DECISION_INTERRUPT)


async def test_timeout_sweep_does_not_open_the_manual_gate(monkeypatch, fake_redis):
    """超时巡检只能用 TIMEOUT 唤醒「等待候选人回复」，不许碰人工闸门。

    这是实测过的事故：闸门挂起期间被超时巡检用 "TIMEOUT" 恢复，
    闸门走 reject 分支 → 候选人被判「AI筛选失败」并收到协商暂停邮件。
    """
    from app.tasks import timeout_tasks

    monkeypatch.setattr(timeout_tasks.settings, "GRAPH_OPEN", True)
    monkeypatch.setattr(timeout_tasks, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(timeout_tasks, "AsyncSessionFactory", lambda: _Session())

    @dataclass
    class _Row:
        candidate_id: str = "cand-001"
        candidate_email: str = "c@example.com"

    class _HistoryRepo:
        def __init__(self, session=None):
            pass

        async def list_stale_negotiating(self, cutoff, limit=200):
            return [_Row()]

    monkeypatch.setattr(timeout_tasks, "CandidateStatusHistoryRepo", _HistoryRepo)

    stub = _StubGraph(interrupts=[Interrupt(value={"node": MANUAL_DECISION_INTERRUPT})])
    monkeypatch.setattr(timeout_tasks, "get_checkpointer", lambda *a, **k: object())
    monkeypatch.setattr(timeout_tasks, "build_candidate_graph", lambda *a, **k: stub)

    swept = await timeout_tasks.sweep_stale_email_replies()

    assert swept == 0
    assert stub.resumed == [], "超时巡检绝不能 resume 人工闸门"


async def test_timeout_sweep_still_opens_email_reply(monkeypatch, fake_redis):
    """对照：挂起的确实是「等待候选人回复」时，超时巡检照常工作。"""
    from app.tasks import timeout_tasks

    monkeypatch.setattr(timeout_tasks.settings, "GRAPH_OPEN", True)
    monkeypatch.setattr(timeout_tasks, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(timeout_tasks, "AsyncSessionFactory", lambda: _Session())

    @dataclass
    class _Row:
        candidate_id: str = "cand-001"
        candidate_email: str = "c@example.com"

    class _HistoryRepo:
        def __init__(self, session=None):
            pass

        async def list_stale_negotiating(self, cutoff, limit=200):
            return [_Row()]

    monkeypatch.setattr(timeout_tasks, "CandidateStatusHistoryRepo", _HistoryRepo)

    stub = _StubGraph(interrupts=[Interrupt(value=EMAIL_REPLY_INTERRUPT)])
    monkeypatch.setattr(timeout_tasks, "get_checkpointer", lambda *a, **k: object())
    monkeypatch.setattr(timeout_tasks, "build_candidate_graph", lambda *a, **k: stub)

    swept = await timeout_tasks.sweep_stale_email_replies()

    assert swept == 1
    assert len(stub.resumed) == 1


def _models_with_status(status):
    from types import SimpleNamespace

    return (
        SimpleNamespace(id="cand-001", email="c@example.com", status=status),
        SimpleNamespace(id="pos-001"),
        SimpleNamespace(id="user-001"),
    )
