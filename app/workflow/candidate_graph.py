from langgraph.graph import StateGraph, START, END
from langgraph.graph.state import CompiledStateGraph

from app.workflow.candidate_state import (
    CandidateRecruitmentState,
    EMAIL_REPLY_TIMEOUT,
    MAX_NEGOTIATION_ROUNDS,
    SCORE_PASS_THRESHOLD,
)
from app.workflow.candidate_nodes.check_availability_node import (
    check_availability_node,
    get_candidate_email_reply_node,
)
from app.workflow.candidate_nodes.confirm_node import confirm_node
from app.workflow.candidate_nodes.email_invite_node import email_invite_node
from app.workflow.candidate_nodes.negotiation_expired_node import negotiation_expired_node
from app.workflow.candidate_nodes.rejected_or_refused_nodes import rejected_node, refused_node
from app.workflow.candidate_nodes.scoring_node import scoring_node
from app.workflow.candidate_nodes.screening_node import screening_node


def route_after_email_reply(state: CandidateRecruitmentState) -> str:
    """get_candidate_email_reply_node 之后的路由。

    - negotiation_expired: 外部调度器超时后 resume 注入 EMAIL_REPLY_TIMEOUT → 终止
    - check_availability: 正常收到候选人回复 → 检查面试官时间可用性
    """
    if state.get("candidate_email_reply") == EMAIL_REPLY_TIMEOUT:
        return "negotiation_expired"
    return "check_availability"


def route_after_check_availability(state: CandidateRecruitmentState) -> str:
    """check_availability 分支路由（含协商轮次上限）。

    - confirmed: 时间可用 → 确认面试
    - refused: 候选人拒绝 → 结束
    - negotiation_expired: 超过协商轮次上限 → 结束
    - get_candidate_email_reply_node: 时间不可用 → 回到等待状态继续协商
    """
    reply = state.get("candidate_email_reply", "")
    if reply == "CANDIDATE_REFUSED":
        return "refused"
    if reply == "SYSTEM_TIME_AVAILABLE":
        return "confirmed"
    # SYSTEM_TIME_UNAVAILABLE 或其他 → 检查协商轮次上限，超限则终止
    if state.get("negotiation_round", 0) >= MAX_NEGOTIATION_ROUNDS:
        return "negotiation_expired"
    return "get_candidate_email_reply_node"


def build_candidate_graph(checkpointer) -> CompiledStateGraph:
    # Pyright/Pylance 无法把 TypedDict 子类与 langgraph 1.x 的 StateLike 协议联合
    # (TypedDictLikeV1 | TypedDictLikeV2 | DataclassLike | BaseModel) 正确匹配，
    # 这是静态检查的误报，运行时正常。
    graph = StateGraph(CandidateRecruitmentState)  # pyright: ignore[reportArgumentType]

    # === 注册节点 ===
    graph.add_node("screening", screening_node)
    graph.add_node("scoring", scoring_node)
    graph.add_node("email_invite", email_invite_node)
    graph.add_node("get_candidate_email_reply_node", get_candidate_email_reply_node)
    graph.add_node("check_availability", check_availability_node)
    graph.add_node("confirm", confirm_node)
    graph.add_node("rejected", rejected_node)
    graph.add_node("refused", refused_node)
    graph.add_node("negotiation_expired", negotiation_expired_node)

    # === 静态边 ===
    graph.add_edge(START, "screening")
    graph.add_edge("rejected", END)
    graph.add_edge("refused", END)
    graph.add_edge("confirm", END)
    graph.add_edge("negotiation_expired", END)

    # === 条件边：screening → scoring 或 rejected ===
    graph.add_conditional_edges(
        "screening",
        lambda s: "scoring" if s["screening_passed"] else "rejected",
        {"scoring": "scoring", "rejected": "rejected"}
    )

    # === 条件边：scoring → email_invite 或 rejected ===
    graph.add_conditional_edges(
        "scoring",
        lambda s: "email_invite" if s["score_result"] and s["score_result"].overall_score > SCORE_PASS_THRESHOLD else "rejected",
        {"email_invite": "email_invite", "rejected": "rejected"}
    )

    # === 邮件发送后等待候选人回复 ===
    graph.add_edge("email_invite", "get_candidate_email_reply_node")

    # === 收到回复：超时恢复标记 → 终止；否则检查面试官时间可用性 ===
    graph.add_conditional_edges(
        "get_candidate_email_reply_node",
        route_after_email_reply,
        {
            "check_availability": "check_availability",
            "negotiation_expired": "negotiation_expired",
        }
    )

    # === 条件边：check_availability 四分支 ===
    graph.add_conditional_edges(
        "check_availability",
        route_after_check_availability,
        {
            "confirmed": "confirm",
            "refused": "refused",
            "get_candidate_email_reply_node": "get_candidate_email_reply_node",
            "negotiation_expired": "negotiation_expired",
        }
    )

    return graph.compile(checkpointer=checkpointer)
