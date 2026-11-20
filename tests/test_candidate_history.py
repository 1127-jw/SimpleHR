"""状态变更链（candidate_status_history）与 thread_id 隔离的纯逻辑测试。

背景：同邮箱的多条候选人记录曾共用一个 ``thread_id``，导致超时巡检用旧记录的
email 唤醒了另一条记录的中断点（见 ``change2026-11-19.md`` §一 / §三）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.workflow import candidate_graph as graph_mod
from app.workflow.candidate_graph import (
    _route_after_scoring,
    route_after_manual_decision,
)
from app.workflow.candidate_state import (
    MANUAL_DECISION_APPROVE,
    MANUAL_DECISION_ARCHIVE,
    MANUAL_DECISION_REJECT,
    thread_id_of,
)


# ---------------- thread_id 隔离 ----------------

def test_thread_id_is_keyed_by_candidate_id_not_email():
    """两条同邮箱的记录必须有不同的 thread_id。

    邮箱重复是常态（同一候选人多次投递），用它当 thread_id 会让
    A 流程的恢复打到 B 流程的中断点上。
    """
    a = thread_id_of("cand-A")
    b = thread_id_of("cand-B")
    assert a != b
    assert a == "candidate:cand-A"


def test_thread_id_is_stable():
    assert thread_id_of("cand-A") == thread_id_of("cand-A")


# ---------------- 跳过筛选直达邀请 ----------------

def _state(**overrides):
    base = {
        "candidate": SimpleNamespace(id="c1"),
        "score_pass_threshold": 7,
        "score_result": None,
    }
    base.update(overrides)
    return base


def test_scoring_routes_to_gate_by_default():
    state = _state(score_result=SimpleNamespace(overall_score=9))
    assert _route_after_scoring(state) == "manual_decision"


def test_scoring_rejects_below_threshold():
    state = _state(score_result=SimpleNamespace(overall_score=2))
    assert _route_after_scoring(state) == "rejected"


def test_skip_scoring_bypasses_the_gate():
    """skip_scoring 的语义是「HR 已经拍板」——不该再停一次人工闸门。"""
    state = _state(skip_scoring=True, score_result=None)
    assert _route_after_scoring(state) == "email_invite"


# ---------------- 人工闸门三选项 ----------------

def test_manual_decision_approve_goes_to_invite():
    state = {"candidate": SimpleNamespace(id="c1"), "manual_decision": MANUAL_DECISION_APPROVE}
    assert route_after_manual_decision(state) == "email_invite"


def test_manual_decision_reject_goes_to_rejected():
    state = {"candidate": SimpleNamespace(id="c1"), "manual_decision": MANUAL_DECISION_REJECT}
    assert route_after_manual_decision(state) == "rejected"


def test_manual_decision_archive_goes_to_archive():
    state = {"candidate": SimpleNamespace(id="c1"), "manual_decision": MANUAL_DECISION_ARCHIVE}
    assert route_after_manual_decision(state) == "archive"


def test_manual_decision_unknown_value_falls_back_to_rejected():
    """任何非 approve / 非 archive 的值都按淘汰处理（保守）。"""
    state = {"candidate": SimpleNamespace(id="c1"), "manual_decision": "TIMEOUT"}
    assert route_after_manual_decision(state) == "rejected"


def test_archive_node_is_registered_in_production_graph():
    compiled = graph_mod.build_candidate_graph(checkpointer=__import__(
        "langgraph.checkpoint.memory", fromlist=["InMemorySaver"]
    ).InMemorySaver())
    nodes = {n for n in compiled.get_graph().nodes}
    assert {"archive", "settle"} <= nodes


# ---------------- 收尾节点的写库参数 ----------------
# 回归：settle_node 曾因给 write_terminal_record 传了它不接受的 from_status 而报
# TypeError，导致「恢复图失败」（候选人收到错误流程）。

async def test_settle_node_writes_terminal_record(monkeypatch):
    from app.workflow.candidate_nodes import settle_node as settle_mod

    captured = {}
    archived = []

    async def _fake_write(candidate, position, **kwargs):
        captured.update(kwargs)

    async def _fake_mark(cid):
        archived.append(cid)

    monkeypatch.setattr(settle_mod, "write_terminal_record", _fake_write)
    monkeypatch.setattr(settle_mod, "mark_candidate_archived", _fake_mark)

    state = {
        "candidate": SimpleNamespace(id="c1", status="面试时间协商中"),
        "position": SimpleNamespace(id="p1", title="后端"),
    }
    result = await settle_mod.settle_node(state)

    assert result["current_stage"] == "settled"
    assert captured["to_status"] == "待面试"
    assert captured["from_status"] == "面试时间协商中"
    assert captured["next_id"] == "-2"
    # archived_at 表示「图流程走完」，结算（正常完成）也要写
    assert archived == ["c1"]


async def test_archive_node_writes_terminal_record_and_marks_archived(monkeypatch):
    from app.workflow.candidate_nodes import archive_node as archive_mod

    captured = {}
    archived = []

    async def _fake_write(candidate, position, **kwargs):
        captured.update(kwargs)

    async def _fake_status(cid):
        return "AI筛选失败"

    async def _fake_mark(cid):
        archived.append(cid)

    monkeypatch.setattr(archive_mod, "write_terminal_record", _fake_write)
    monkeypatch.setattr(archive_mod, "_load_current_status", _fake_status)
    monkeypatch.setattr(archive_mod, "mark_candidate_archived", _fake_mark)

    state = {
        "candidate": SimpleNamespace(id="c1", status="已投递"),
        "position": SimpleNamespace(id="p1", title="后端"),
    }
    result = await archive_mod.archive_node(state)

    assert result["current_stage"] == "archived"
    assert captured["to_status"] == "AI筛选失败", "归档记录的是库里的当前细分状态"
    assert captured["next_id"] == "-1"
    assert archived == ["c1"]


# ---------------- 邀请邮件的备选时段 ----------------

async def _coro(value):
    return value


def test_fmt_slot_formats_wall_clock_time():
    from datetime import datetime, timedelta, timezone

    from app.workflow.candidate_nodes.email_invite_node import _fmt_slot

    dt = datetime(2026, 11, 20, 9, 0, tzinfo=timezone(timedelta(hours=8)))
    assert _fmt_slot(dt) == "2026-11-20 09:00"


async def test_invite_email_lists_up_to_three_slots(monkeypatch):
    """邀请正文要列出最多 3 段可用时间，避免候选人往返问备选。"""
    from datetime import datetime, timedelta, timezone

    from app.workflow.candidate_nodes import email_invite_node as mod

    tz = timezone(timedelta(hours=8))
    slots = [
        (datetime(2026, 11, 20, 9, 0, tzinfo=tz), datetime(2026, 11, 20, 10, 0, tzinfo=tz)),
        (datetime(2026, 11, 20, 14, 0, tzinfo=tz), datetime(2026, 11, 20, 15, 0, tzinfo=tz)),
        (datetime(2026, 11, 21, 10, 0, tzinfo=tz), datetime(2026, 11, 21, 11, 0, tzinfo=tz)),
        (datetime(2026, 11, 22, 10, 0, tzinfo=tz), datetime(2026, 11, 22, 11, 0, tzinfo=tz)),
    ]

    monkeypatch.setattr(mod, "get_user_available_slots", lambda *a, **k: _coro(slots))
    monkeypatch.setattr(mod, "_mark_invite_sent", lambda *a, **k: _coro(None))
    monkeypatch.setattr(mod, "_enter_negotiation", lambda *a, **k: _coro(None))

    sent = {}

    async def _capture(to, *, subject, text):
        sent["text"] = text

    monkeypatch.setattr(mod, "send_workflow_email", _capture)

    state = {
        "candidate": SimpleNamespace(
            id="c1", name="张三", email="a@qq.com", status="AI筛选成功"
        ),
        "position": SimpleNamespace(id="p1", title="后端"),
        "interviewer": SimpleNamespace(id="u1"),
    }
    await mod.email_invite_node(state)

    text = sent["text"]
    assert "2026-11-20 09:00" in text and "2026-11-20 10:00" in text
    assert "2026-11-21 10:00" in text
    assert "2026-11-22" not in text, "最多列 3 段"
    assert "第一个时间段" in text


# ---------------- 时段不可用时的备选时段 ----------------

async def test_alternative_slots_formatted_and_capped(monkeypatch):
    """时段不可用时的回信要带备选时段，且最多 3 段。"""
    from datetime import datetime, timedelta, timezone

    from app.workflow.candidate_nodes import check_availability_node as cav

    tz = timezone(timedelta(hours=8))
    slots = [
        (datetime(2026, 11, 20, 9, 0, tzinfo=tz), datetime(2026, 11, 20, 10, 0, tzinfo=tz)),
        (datetime(2026, 11, 21, 10, 0, tzinfo=tz), datetime(2026, 11, 21, 11, 0, tzinfo=tz)),
        (datetime(2026, 11, 22, 10, 0, tzinfo=tz), datetime(2026, 11, 22, 11, 0, tzinfo=tz)),
        (datetime(2026, 11, 23, 10, 0, tzinfo=tz), datetime(2026, 11, 23, 11, 0, tzinfo=tz)),
    ]
    monkeypatch.setattr(cav, "get_user_available_slots", lambda *a, **k: _coro(slots))

    text = await cav._load_alternative_slots(
        {"candidate": SimpleNamespace(id="c1"), "interviewer": SimpleNamespace(id="u1")}
    )
    assert "1. 2026-11-20 09:00 - 2026-11-20 10:00" in text
    assert "2026-11-23" not in text, "最多 3 段"


async def test_alternative_slots_empty_on_failure(monkeypatch):
    """查不到可用时段时返回空串，不影响回信（best-effort）。"""
    from app.workflow.candidate_nodes import check_availability_node as cav

    def _boom(*a, **k):
        raise RuntimeError("dingtalk down")

    monkeypatch.setattr(cav, "get_user_available_slots", _boom)
    text = await cav._load_alternative_slots(
        {"candidate": SimpleNamespace(id="c1"), "interviewer": SimpleNamespace(id="u1")}
    )
    assert text == ""


# ---------------- 终态节点单独写链 ----------------

async def test_rejected_node_records_chain(monkeypatch):
    """淘汰节点要自己写状态变更链，不能只靠 archive_node 补终态。"""
    from types import SimpleNamespace as NS

    from app.workflow.candidate_nodes import rejected_node as mod

    captured = {}

    async def _fake(candidate, position, to_status, **kw):
        captured["id"] = candidate.id
        captured["status"] = getattr(to_status, "value", to_status)

    monkeypatch.setattr(mod, "record_node_transition", _fake)

    await mod.rejected_node(
        {"candidate": NS(id="c1", status="已投递"), "position": NS(id="p1")}
    )
    assert captured == {"id": "c1", "status": "AI筛选失败"}


async def test_refused_node_records_chain(monkeypatch):
    from types import SimpleNamespace as NS

    from app.workflow.candidate_nodes import refused_node as mod

    captured = {}

    async def _fake(candidate, position, to_status, **kw):
        captured["status"] = getattr(to_status, "value", to_status)

    monkeypatch.setattr(mod, "record_node_transition", _fake)
    await mod.refused_node(
        {"candidate": NS(id="c1", status="面试时间协商中"), "position": NS(id="p1")}
    )
    assert captured["status"] == "拒绝面试"


async def test_screening_failure_records_chain(monkeypatch):
    """筛选未通过也要留痕，链里能看到「为什么没过」。"""
    from types import SimpleNamespace as NS

    from app.workflow.candidate_nodes import screening_node as mod

    captured = {}

    async def _fake(candidate, position, to_status, **kw):
        captured["status"] = getattr(to_status, "value", to_status)

    monkeypatch.setattr(mod, "record_node_transition", _fake)

    candidate = NS(id="c1", work_year=1, email="a@qq.com")
    position = NS(id="p1", work_year=5)
    result = await mod.screening_node(
        {"candidate": candidate, "position": position}
    )
    assert result["screening_passed"] is False
    assert captured["status"] == "AI筛选失败"


# ---------------- skip_scoring 权限 ----------------

async def test_skip_scoring_forbidden_for_non_hr(monkeypatch):
    """跳过 AI 评估只允许 HR / 超管，普通员工传了也拒绝。"""
    from types import SimpleNamespace as NS

    from fastapi import HTTPException

    import importlib

    router_mod = importlib.import_module("app.routers.candidate_router")

    async def _fake_ser(*a, **k):
        return NS(id="c1"), NS(id="p1"), NS(id="u1")

    monkeypatch.setattr(router_mod, "create_candidate_ser", _fake_ser)

    with pytest.raises(HTTPException) as excinfo:
        await router_mod.create_candidate(
            candidate_data=NS(skip_scoring=True),
            session=None,
            current_user=NS(id="u1", is_hr=False, is_superuser=False),
            background_tasks=NS(add_task=lambda *a, **k: None),
        )
    assert excinfo.value.status_code == 403


async def test_skip_scoring_allowed_for_hr(monkeypatch):
    from types import SimpleNamespace as NS

    import importlib

    router_mod = importlib.import_module("app.routers.candidate_router")

    monkeypatch.setattr(
        router_mod, "create_candidate_ser",
        lambda *a, **k: _coro((NS(id="c1"), NS(id="p1"), NS(id="u1"))),
    )
    scheduled = []
    result = await router_mod.create_candidate(
        candidate_data=NS(skip_scoring=True),
        session=None,
        current_user=NS(id="u1", is_hr=True, is_superuser=False),
        background_tasks=NS(add_task=lambda *a, **k: scheduled.append(a)),
    )
    assert result is not None
    assert scheduled, "HR 勾了跳过筛选应正常起图"
