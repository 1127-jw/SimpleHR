import logging
from langgraph.graph import StateGraph, START, END
from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger(__name__)

from app.workflow.candidate_state import (
    CandidateRecruitmentState,
    EMAIL_REPLY_TIMEOUT,
    MANUAL_DECISION_APPROVE,
    REPLY_REFUSED,
    REPLY_SLOT_TAKEN,
    REPLY_TIME_AVAILABLE,
    DEFAULT_SCORE_PASS_THRESHOLD,
    DEFAULT_MAX_NEGOTIATION_ROUNDS,
)
from app.workflow.candidate_nodes.check_availability_node import check_availability_node
from app.workflow.candidate_nodes.confirm_node import confirm_node
from app.workflow.candidate_nodes.email_invite_node import email_invite_node
from app.workflow.candidate_nodes.error_node import error_node
from app.workflow.candidate_nodes.get_candidate_email_reply_node import (
    get_candidate_email_reply_node,
)
from app.workflow.candidate_nodes.manual_decision_node import manual_decision_node
from app.workflow.candidate_nodes.negotiation_expired_node import negotiation_expired_node
from app.workflow.candidate_nodes.recheck_interview_slot_node import (
    recheck_interview_slot_node,
)
from app.workflow.candidate_nodes.refused_node import refused_node
from app.workflow.candidate_nodes.rejected_node import rejected_node
from app.workflow.candidate_nodes.scoring_node import scoring_node
from app.workflow.candidate_nodes.screening_node import screening_node


def _with_error_check(router):
    """给路由函数加一层「节点已标记错误 → 直接去 error 节点」的守卫。"""

    def guarded(state: CandidateRecruitmentState) -> str:
        if state.get("error_occurred"):
            return "error"
        return router(state)

    return guarded


def _route_after_screening(state: CandidateRecruitmentState) -> str:
    """screening 节点之后的路由：通过去评分，否则淘汰。"""
    cid = state["candidate"].id
    passed = bool(state.get("screening_passed"))
    dest = "scoring" if passed else "rejected"
    logger.info(
        "[route_screening] candidate_id=%s | passed=%s | dest=%s",
        cid, passed, dest,
    )
    return dest


def route_after_email_reply(state: CandidateRecruitmentState) -> str:
    """get_candidate_email_reply_node 之后的路由。"""
    cid = state["candidate"].id
    reply = state.get("candidate_email_reply")
    dest = "negotiation_expired" if reply == EMAIL_REPLY_TIMEOUT else "check_availability"
    logger.info(
        "[route_email_reply] candidate_id=%s | reply=%.50s | dest=%s",
        cid, str(reply), dest,
    )
    return dest


def _route_after_email_invite(state: CandidateRecruitmentState) -> str:
    """email_invite 之后的路由。

    ``email_invite_node`` 发信失败时不会抛到图外（``BusinessException`` 被
    ``node_error_wrapper`` 转成 ``error_occurred=True`` 的状态更新），若这里用静态边，
    流程照样会走到中断点并把 ``current_stage`` 覆盖成 ``email_reply``，而
    ``email_invite_sent_at`` 仍是空 —— 超时巡检按该字段筛选，捞不到他，
    候选人于是永久停在「AI 筛选成功」。所以这条边必须带错误守卫。
    """
    cid = state["candidate"].id
    logger.info("[route_email_invite] candidate_id=%s | dest=get_candidate_email_reply_node", cid)
    return "get_candidate_email_reply_node"


def route_after_check_availability(state: CandidateRecruitmentState) -> str:
    """check_availability 分支路由（含协商轮次上限）。"""
    cid = state["candidate"].id
    reply = state.get("candidate_email_reply", "")
    if reply == REPLY_REFUSED:
        dest = "refused"
    elif reply == REPLY_TIME_AVAILABLE:
        dest = "confirmed"
    else:
        max_rounds = state.get("max_negotiation_rounds") or DEFAULT_MAX_NEGOTIATION_ROUNDS
        if state.get("negotiation_round", 0) >= max_rounds:
            dest = "negotiation_expired"
        else:
            dest = "get_candidate_email_reply_node"
    logger.info(
        "[route_check_avail] candidate_id=%s | reply=%.30s | round=%d | dest=%s",
        cid, reply, state.get("negotiation_round", 0), dest,
    )
    return dest


def route_after_recheck_interview_slot(state: CandidateRecruitmentState) -> str:
    """复核节点之后的路由。

    复核节点已经把 ``candidate_email_reply`` 写成
    ``REPLY_TIME_AVAILABLE``（仍空闲）或 ``REPLY_SLOT_TAKEN``（被占用）。
    被占用时节点已把 ``negotiation_round`` 归零并重新发了约时间的邮件，
    这里只需把流程带回等待回复的中断点。
    """
    cid = state["candidate"].id
    reply = state.get("candidate_email_reply")
    dest = "get_candidate_email_reply_node" if reply == REPLY_SLOT_TAKEN else "confirm"
    logger.info(
        "[route_recheck] candidate_id=%s | reply=%.30s | round=%d | dest=%s",
        cid, str(reply), state.get("negotiation_round", 0), dest,
    )
    return dest


def route_after_manual_decision(state: CandidateRecruitmentState) -> str:
    """人工闸门之后的路由：确认发送 → 邀请邮件；否则淘汰。"""
    cid = state["candidate"].id
    decision = state.get("manual_decision")
    dest = "email_invite" if decision == MANUAL_DECISION_APPROVE else "rejected"
    logger.info(
        "[route_manual_decision] candidate_id=%s | decision=%s | dest=%s",
        cid, decision, dest,
    )
    return dest


def _route_after_scoring(state: CandidateRecruitmentState) -> str:
    """scoring 节点之后的路由：达阈值进人工闸门，否则淘汰。

    通过之后**不直接**发邀请邮件，而是先停在 ``manual_decision`` 由 HR 拍板。
    """
    cid = state["candidate"].id
    threshold = state.get("score_pass_threshold") or DEFAULT_SCORE_PASS_THRESHOLD
    score_result = state.get("score_result")
    passed = score_result and score_result.overall_score > threshold
    dest = "manual_decision" if passed else "rejected"
    logger.info(
        "[route_scoring] candidate_id=%s | score=%s | threshold=%d | dest=%s",
        cid, score_result.overall_score if score_result else None, threshold, dest,
    )
    return dest


def build_candidate_graph(
    checkpointer,
    *,
    score_pass_threshold: int | None = None,
    max_negotiation_rounds: int | None = None,
) -> CompiledStateGraph:
    """编译候选人招聘流程状态图。

    所有节点函数已在各自模块中用 ``@node_error_wrapper`` 装饰，
    此处直接注册原始引用即可，无需额外包装。

    Args:
        checkpointer: LangGraph 检查点存储器。
        score_pass_threshold: AI 评分通过阈值，默认取 Settings.SCORE_PASS_THRESHOLD。
        max_negotiation_rounds: 协商轮次上限，默认取 Settings.MAX_NEGOTIATION_ROUNDS。
    """
    graph = StateGraph(CandidateRecruitmentState)

    # === 注册节点（已在各模块中用 @node_error_wrapper 装饰） ===
    graph.add_node("screening", screening_node)
    graph.add_node("scoring", scoring_node)
    graph.add_node("manual_decision", manual_decision_node)
    graph.add_node("email_invite", email_invite_node)
    graph.add_node("get_candidate_email_reply_node", get_candidate_email_reply_node)
    graph.add_node("check_availability", check_availability_node)
    graph.add_node("recheck_interview_slot", recheck_interview_slot_node)
    graph.add_node("confirm", confirm_node)
    graph.add_node("rejected", rejected_node)
    graph.add_node("refused", refused_node)
    graph.add_node("negotiation_expired", negotiation_expired_node)
    graph.add_node("error", error_node)

    # === 静态边 ===
    graph.add_edge(START, "screening")
    graph.add_edge("rejected", END)
    graph.add_edge("refused", END)
    graph.add_edge("confirm", END)
    graph.add_edge("negotiation_expired", END)
    graph.add_edge("error", END)

    # === 条件边：screening → scoring 或 rejected ===
    graph.add_conditional_edges(
        "screening",
        _with_error_check(_route_after_screening),
        {"scoring": "scoring", "rejected": "rejected", "error": "error"}
    )

    # === 条件边：scoring → 人工闸门 或 rejected ===
    graph.add_conditional_edges(
        "scoring",
        _with_error_check(_route_after_scoring),
        {
            "manual_decision": "manual_decision",
            "rejected": "rejected",
            "error": "error",
        }
    )

    # === 条件边：人工闸门 → 邀请邮件 或 rejected ===
    graph.add_conditional_edges(
        "manual_decision",
        _with_error_check(route_after_manual_decision),
        {
            "email_invite": "email_invite",
            "rejected": "rejected",
            "error": "error",
        }
    )

    # === 邮件发送后等待候选人回复（条件边：发信失败要在进入中断点之前分流到 error） ===
    graph.add_conditional_edges(
        "email_invite",
        _with_error_check(_route_after_email_invite),
        {
            "get_candidate_email_reply_node": "get_candidate_email_reply_node",
            "error": "error",
        }
    )

    # === 收到回复 ===
    graph.add_conditional_edges(
        "get_candidate_email_reply_node",
        _with_error_check(route_after_email_reply),
        {
            "check_availability": "check_availability",
            "negotiation_expired": "negotiation_expired",
            "error": "error",
        }
    )

    # === 条件边：check_availability 四分支 ===
    # 「确认时间」不再直接进 confirm：中间插一道复核（该时段可能在协商期间被占）
    graph.add_conditional_edges(
        "check_availability",
        _with_error_check(route_after_check_availability),
        {
            "confirmed": "recheck_interview_slot",
            "refused": "refused",
            "get_candidate_email_reply_node": "get_candidate_email_reply_node",
            "negotiation_expired": "negotiation_expired",
            "error": "error",
        }
    )

    # === 条件边：复核 → 建日程 或 回到等待回复 ===
    graph.add_conditional_edges(
        "recheck_interview_slot",
        _with_error_check(route_after_recheck_interview_slot),
        {
            "confirm": "confirm",
            "get_candidate_email_reply_node": "get_candidate_email_reply_node",
            "error": "error",
        }
    )

    compiled = graph.compile(checkpointer=checkpointer)

    # 将阈值附加到编译图，供 ainvoke 时注入初始状态
    compiled._score_pass_threshold = score_pass_threshold or DEFAULT_SCORE_PASS_THRESHOLD
    compiled._max_negotiation_rounds = max_negotiation_rounds or DEFAULT_MAX_NEGOTIATION_ROUNDS

    return compiled
