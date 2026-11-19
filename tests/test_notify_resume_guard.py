"""回归：回复邮件恢复流程时，只允许 resume「等待候选人回复」这个中断点。

事故：候选人回复邀请后收到**两封**邮件——一封「面试时间不可用」、一封邀请。
原因是 ``notify`` 用 ``Command(resume=...)`` 恢复时，thread 的中断点已被消费
（或状态异常），LangGraph 让图**从头重跑**；state 里残留的 ``skip_scoring=True``
又让 scoring 直达 email_invite，于是再发了一次邀请。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from langgraph.types import Command, Interrupt

from app.models.candidate import CandidateStatusEnum
from app.workflow.candidate_state import EMAIL_REPLY_INTERRUPT
from utils.emailbot import notify as notify_mod


class _StubGraph:
    def __init__(self, interrupts):
        self._interrupts = interrupts
        self.resumed = []

    async def aget_state(self, config):
        return SimpleNamespace(interrupts=self._interrupts)

    async def ainvoke(self, cmd, config):
        self.resumed.append(cmd)
        return None


def _candidate():
    return SimpleNamespace(
        id="cand-001",
        email="c@example.com",
        status=CandidateStatusEnum.NEGOTIATING.value,
    )


async def test_resume_skipped_when_interrupt_is_not_email_reply(monkeypatch):
    """中断点是别的（例如人工闸门）时，绝不能用回复内容去 resume。"""
    stub = _StubGraph(interrupts=[Interrupt(value={"node": "manual_decision"})])
    monkeypatch.setattr(notify_mod, "_graph", stub)
    monkeypatch.setattr(notify_mod.settings, "GRAPH_OPEN", True)

    await notify_mod._resume_candidate_graph(
        email=SimpleNamespace(subject="s", text="t"),
        candidate_schema=_candidate(),
        position_schema=SimpleNamespace(id="p1"),
        interviewer_schema=SimpleNamespace(id="u1"),
    )

    assert stub.resumed == [], "非「等待候选人回复」的中断点不能被 resume"


async def test_resume_proceeds_for_email_reply_interrupt(monkeypatch):
    stub = _StubGraph(interrupts=[Interrupt(value=EMAIL_REPLY_INTERRUPT)])
    monkeypatch.setattr(notify_mod, "_graph", stub)
    monkeypatch.setattr(notify_mod.settings, "GRAPH_OPEN", True)
    from langgraph.checkpoint.memory import InMemorySaver

    monkeypatch.setattr(notify_mod, "get_checkpointer", lambda *a, **k: InMemorySaver())

    class _Slot:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(
        "app.core.limiters.task_slot", lambda *a, **k: _Slot(), raising=False
    )

    await notify_mod._resume_candidate_graph(
        email=SimpleNamespace(subject="s", text="t"),
        candidate_schema=_candidate(),
        position_schema=SimpleNamespace(id="p1"),
        interviewer_schema=SimpleNamespace(id="u1"),
    )

    assert len(stub.resumed) == 1
    assert isinstance(stub.resumed[0], Command)


async def test_manual_retry_forces_skip_scoring(monkeypatch):
    """重试的 ``update`` 必须带 skip_scoring=True。

    实测 ``Command(goto="email_invite")`` 在这条路径上仍会先经过
    screening / scoring（日志里能看到 route_screening）。不跳过的后果：
    重复插入 AI 评分（撞唯一索引）+ 再次发邀请 / 再进一次人工闸门。
    """
    import app.service.candidate_status_ser as ser

    captured = {}

    class _StubGraph:
        async def ainvoke(self, cmd, config):
            captured["cmd"] = cmd
            return None

    async def _fake_load(cid):
        return (
            SimpleNamespace(id="cand-001", email="c@example.com",
                            status=CandidateStatusEnum.AI_FILTER_FAILED.value),
            SimpleNamespace(id="pos-001", title="后端"),
            SimpleNamespace(id="user-001"),
        )

    monkeypatch.setattr(ser, "_load_graph_models", _fake_load)
    monkeypatch.setattr(ser, "get_checkpointer", lambda *a, **k: object())

    class _HistoryRepo:
        def __init__(self, session=None):
            pass

        async def start_new_chain(self, *a, **k):
            return None

    monkeypatch.setattr(ser, "CandidateStatusHistoryRepo", _HistoryRepo)
    monkeypatch.setattr(ser, "AsyncSessionFactory", lambda: _FakeSession())
    monkeypatch.setattr(ser, "build_candidate_graph", lambda *a, **k: _StubGraph())

    class _Slot:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(
        "app.core.limiters.task_slot", lambda *a, **k: _Slot(), raising=False
    )

    await ser.trigger_manual_retry("cand-001")

    update = captured["cmd"].update
    assert update["skip_scoring"] is True, "重试必须跳过 scoring，否则会重复插入评分/重复发邀请"
    assert update["current_stage"] == "email_invite"


class _FakeSession:
    def begin(self):
        return _Tx()

    async def execute(self, *args, **kwargs):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False
