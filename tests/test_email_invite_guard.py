"""``email_invite`` 的出边必须带错误守卫。

背景：发信失败时 ``@node_error_wrapper`` 只把 ``error_occurred=True`` 写回状态、
异常不外抛，而 ``email_invite → get_candidate_email_reply_node`` 曾是**静态边**，
不查这个标记 —— 于是流程照旧挂到中断点等一封从未发出的邮件：候选人状态停在
「AI 筛选成功」，``current_stage`` 却被中断点覆盖成 ``email_reply``，
``email_sent_at`` 不写入，超时巡检也就捞不到他。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph

from app.core.exceptions import EmailSendError
from app.workflow import candidate_graph as graph_mod
from app.workflow.candidate_nodes import email_invite_node as invite_mod
from app.workflow.candidate_state import CandidateRecruitmentState


@dataclass
class _Candidate:
    id: str
    name: str
    email: str


@dataclass
class _Position:
    title: str


@dataclass
class _Interviewer:
    id: str
    email: str


def _state(**overrides) -> dict:
    state = {
        "candidate": _Candidate(id="cand-001", name="张三", email="c@example.com"),
        "position": _Position(title="后端工程师"),
        "interviewer": _Interviewer(id="user-001", email="hr@example.com"),
        "current_stage": "scoring",
    }
    state.update(overrides)
    return state


FREE_SLOTS = [
    (datetime(2026, 11, 20, 10, 0), datetime(2026, 11, 20, 11, 0)),
    (datetime(2026, 11, 20, 14, 0), datetime(2026, 11, 20, 15, 0)),
]


class _FailingBot:
    is_connected = True

    async def connect(self) -> None:
        pass

    async def send_email(self, **kwargs) -> None:
        raise EmailSendError("SMTP 连接被重置")


class _OkBot:
    is_connected = True

    def __init__(self) -> None:
        self.sent: list = []

    async def send_email(self, **kwargs) -> None:
        self.sent.append(kwargs)


@pytest.fixture
def send_fails(monkeypatch):
    """让真实 ``email_invite_node`` 走到「发信失败」这条路径。

    空闲时段查询返回正常结果，失败只发生在 ``send_email``，
    因此成功路径末尾那段写库的 ``_mark_invite_sent`` 不会被触及。
    """
    async def _fake_slots(*args, **kwargs):
        return FREE_SLOTS

    monkeypatch.setattr(invite_mod, "get_user_available_slots", _fake_slots)
    monkeypatch.setattr("utils.emailbot.mail_manager.get_mail_bot_", lambda: _FailingBot())


# ---------------- 前置事实：节点不上抛异常，只写标记 ----------------

async def test_failed_invite_only_marks_error_state(send_fails):
    """守卫之所以必须存在，是因为异常在这里就被消化掉了。"""
    result = await invite_mod.email_invite_node(_state())

    assert result["error_occurred"] is True
    assert result["current_stage"] == "error"
    assert result["error_stage"] == "scoring"
    assert "email_sent" not in result


# ---------------- 路由函数 ----------------

def test_router_sends_ok_to_wait_node():
    assert graph_mod._route_after_email_invite(_state()) == "get_candidate_email_reply_node"


def test_router_after_failed_invite_is_guarded():
    guarded = graph_mod._with_error_check(graph_mod._route_after_email_invite)

    assert guarded(_state(error_occurred=True)) == "error"
    assert guarded(_state(error_occurred=False)) == "get_candidate_email_reply_node"


# ---------------- 图层面 ----------------

def _build_invite_graph(reached: list):
    """真实的 email_invite 节点 + 真实的出边判定，下游节点用替身。"""

    async def wait_node(state):
        reached.append("wait")
        return {}

    async def error_node_stub(state):
        reached.append("error")
        return {}

    builder = StateGraph(CandidateRecruitmentState)
    builder.add_node("email_invite", invite_mod.email_invite_node)
    builder.add_node("get_candidate_email_reply_node", wait_node)
    builder.add_node("error", error_node_stub)
    builder.add_edge(START, "email_invite")
    builder.add_conditional_edges(
        "email_invite",
        graph_mod._with_error_check(graph_mod._route_after_email_invite),
        {
            "get_candidate_email_reply_node": "get_candidate_email_reply_node",
            "error": "error",
        },
    )
    return builder.compile(checkpointer=InMemorySaver())


async def test_graph_stops_at_error_when_send_fails(send_fails):
    reached: list = []
    graph = _build_invite_graph(reached)

    await graph.ainvoke(_state(), {"configurable": {"thread_id": uuid.uuid4().hex}})

    assert reached == ["error"], f"发信失败应落到 error 节点，实际走到 {reached}"


async def test_graph_waits_for_reply_when_send_succeeds(monkeypatch):
    """对照：发信成功仍要进入等待回复处，守卫不能写成一律短路。"""

    async def _slots(*args, **kwargs):
        return FREE_SLOTS

    async def _mark_sent(*args, **kwargs):
        return None

    bot = _OkBot()
    monkeypatch.setattr(invite_mod, "get_user_available_slots", _slots)
    monkeypatch.setattr("utils.emailbot.mail_manager.get_mail_bot_", lambda: bot)
    # 成功路径末尾要写「邀请已发送」时间戳，测试里没有数据库
    monkeypatch.setattr(invite_mod, "_mark_invite_sent", _mark_sent)

    reached: list = []
    graph = _build_invite_graph(reached)
    await graph.ainvoke(_state(), {"configurable": {"thread_id": uuid.uuid4().hex}})

    assert reached == ["wait"]
    assert len(bot.sent) == 1


def test_production_graph_wires_email_invite_conditionally():
    """守卫必须真的接在生产图上。

    上面的图层面测试自己搭边，把 ``build_candidate_graph`` 改回 ``add_edge``
    它照样能过——这条直接检查编译出的边，静态边一旦回归就在这里失败。
    """
    compiled = graph_mod.build_candidate_graph(checkpointer=InMemorySaver())
    edges = [e for e in compiled.get_graph().edges if e.source == "email_invite"]

    assert {e.target for e in edges} == {"get_candidate_email_reply_node", "error"}
    assert all(e.conditional for e in edges), "email_invite 出边必须是条件边，否则错误守卫不生效"
