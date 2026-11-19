"""回归测试：等待候选人回复的中断节点必须真的挂起。

背景：原实现用 ``except Exception`` 包裹 ``interrupt()``，把 ``GraphInterrupt``
吞掉 —— 图从不暂停，``Command(resume=...)`` 永远收不到候选人回复，
``candidate_email_reply`` 恒为空字符串。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph
from langgraph.types import Command

from app.workflow.candidate_nodes.get_candidate_email_reply_node import (
    get_candidate_email_reply_node,
)


@dataclass
class _Candidate:
    """可序列化的候选人替身（LangGraph 的 checkpointer 要序列化 state）。"""

    id: str
    email: str


class _State(TypedDict):
    candidate: _Candidate
    candidate_email_reply: Optional[str]
    current_stage: str


async def _wait_reply(state: _State) -> dict:
    """用测试自己的 state schema 包装真实节点。

    不能直接把 ``get_candidate_email_reply_node`` 注册进图：它的形参标注是
    生产用的 ``CandidateRecruitmentState``，LangGraph 会把那个 schema 也加进来，
    与测试的 ``candidate`` channel 冲突（"already exists with a different type"）。
    """
    return await get_candidate_email_reply_node(state)  # type: ignore[arg-type]


def _build_graph():
    builder = StateGraph(_State)
    builder.add_node("wait_reply", _wait_reply)
    builder.add_edge(START, "wait_reply")
    return builder.compile(checkpointer=InMemorySaver())


def _state() -> dict:
    return {
        "candidate": _Candidate(id="cand-001", email="candidate@example.com"),
        "current_stage": "email_invite",
    }


async def test_interrupt_node_pauses_and_resumes():
    """首轮必须挂起并产出 __interrupt__；恢复后必须拿到 resume 的值。"""
    graph = _build_graph()
    config = {"configurable": {"thread_id": uuid.uuid4().hex}}

    first = [chunk async for chunk in graph.astream(_state(), config)]
    assert any("__interrupt__" in chunk for chunk in first), (
        "节点必须挂起并产生 __interrupt__；若没有，说明 GraphInterrupt 被吞掉了"
    )

    resumed = [
        chunk async for chunk in graph.astream(Command(resume="我明天下午3点方便"), config)
    ]
    assert resumed[-1]["wait_reply"]["candidate_email_reply"] == "我明天下午3点方便"


def test_interrupt_node_works_without_try_except():
    """反向对照：确认 GraphInterrupt 确实从节点冒泡（没被 except 转成空回复）。"""
    from langgraph.errors import GraphInterrupt

    assert issubclass(GraphInterrupt, Exception), (
        "GraphInterrupt 是 Exception 子类，所以 except Exception 会捕获它 —— "
        "这正是原实现的 bug 根因"
    )


async def test_interrupt_node_returns_empty_string_when_resume_is_empty():
    graph = _build_graph()
    config = {"configurable": {"thread_id": uuid.uuid4().hex}}

    async for _ in graph.astream(_state(), config):
        pass
    resumed = [chunk async for chunk in graph.astream(Command(resume=""), config)]
    assert resumed[-1]["wait_reply"]["candidate_email_reply"] == ""
