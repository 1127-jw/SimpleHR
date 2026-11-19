from datetime import datetime
from typing import List, Optional, Literal, Annotated
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage
from app.schemas.candidate_schema import CandidateSchema
from app.schemas.position_schema import PositionSchema
from app.schemas.user_schema import UserSH
from app.schemas.agent_schema import AgentCandidateScoreSchema
from app.core.config import get_settings

settings = get_settings()

# === 默认值（来自 Settings，可配置） ===
DEFAULT_SCORE_PASS_THRESHOLD: int = settings.SCORE_PASS_THRESHOLD
DEFAULT_MAX_NEGOTIATION_ROUNDS: int = settings.MAX_NEGOTIATION_ROUNDS
EMAIL_REPLY_TIMEOUT: str = settings.EMAIL_REPLY_TIMEOUT

# === 候选人邮件回复的语义标记 ===
# 由节点写入，由 candidate_graph 的路由函数读取。
# 两端共用同一份常量，避免改一处漏一处。
REPLY_REFUSED: str = "CANDIDATE_REFUSED"
REPLY_TIME_AVAILABLE: str = "SYSTEM_TIME_AVAILABLE"
# 复核节点：候选人确认的时段在创建日程前被面试官的其他日程占用
REPLY_SLOT_TAKEN: str = "SYSTEM_SLOT_TAKEN"

# === 人工闸门（manual_decision）的回复值 ===
# 由前端「发送邮件 / 不发送 / 归档」三个动作传入 Command(resume=...)，
# 节点据此决定继续发邀请、淘汰，还是直接归档收尾。
MANUAL_DECISION_APPROVE: str = "approve"
MANUAL_DECISION_REJECT: str = "reject"
MANUAL_DECISION_ARCHIVE: str = "archive"

# === 中断点标记 ===
# ``interrupt(value)`` 的 ``value`` 会原样出现在 ``StateSnapshot.interrupts`` 里。
# 所有 ``interrupt()`` 节点**共用一个 resume 通道**：谁都能用 ``Command(resume=...)``
# 把任何一个中断点唤醒。恢复方必须用下面的标记确认「这个中断点是我该处理的那个」，
# 否则就会互相误开——实测过：人工闸门被超时巡检用 "TIMEOUT" 唤醒，
# 候选人当场被判成「不发送」并淘汰。
EMAIL_REPLY_INTERRUPT: str = "candidate_email_reply"
MANUAL_DECISION_INTERRUPT: str = "manual_decision"


def thread_id_of(candidate_id: str) -> str:
    """检查点 ``thread_id``：**按候选人记录隔离，不用邮箱**。

    同一候选人多次投递会产生多条记录，而邮箱只有一份；用邮箱当 thread_id 时，
    超时巡检会用「另一条记录」的 email 去 resume，实际唤醒的是别人的中断点。
    实测事故：旧记录的超时唤醒了新建记录的闸门。

    归档 / 结算后重新发起仍是同一个 thread（``Command(goto=...)`` 可以重新进入
    已结束的流程，不需要新图），只是链表会追加一条新记录。
    """
    return f"candidate:{candidate_id}"


def is_waiting_on(interrupts, marker: str) -> bool:
    """当前挂起的中断点是否属于 ``marker`` 对应的节点。

    Args:
        interrupts: ``StateSnapshot.interrupts``（``tuple[Interrupt, ...]``）。
        marker: ``EMAIL_REPLY_INTERRUPT`` 或 ``MANUAL_DECISION_INTERRUPT``。

    ``Interrupt`` 只带 ``value``，不带节点名，所以用 ``interrupt()`` 的入参做身份。
    入参可以是字符串（``interrupt("candidate_email_reply")``）或带 ``node``
    字段的字典（``interrupt({"node": "manual_decision", ...})``），两者都认。
    """
    for it in interrupts or ():
        value = getattr(it, "value", None)
        if value == marker:
            return True
        if isinstance(value, dict) and value.get("node") == marker:
            return True
    return False

# 后写覆盖先写。
#
# 不要用 ``y if y is not None else x``：LangGraph 只在**节点确实写了这个字段**时
# 才调用 reducer，所以「没写」本来就靠默认保留旧值，不需要靠 None 兜底。
# 反过来把 None 当成「不写」会让显式清空失效——人工重试时
# （candidate_status_ser）想清掉的 error_message / candidate_email_reply /
# proposed_interview_time / email_sent_at 全部留在检查点里，下一轮路由读到脏值。
def assign_state_property(left, right):
    return right


class CandidateRecruitmentState(TypedDict):
    # === 基础信息（进入图时设置，全程不变） ===
    candidate: Annotated[CandidateSchema, assign_state_property]
    position: Annotated[PositionSchema, assign_state_property]
    interviewer: Annotated[UserSH, assign_state_property]
    messages: Annotated[List[BaseMessage], add_messages]

    # === 各阶段产出（每个节点写入自己的字段） ===
    # screening 筛选阶段
    screening_passed: Annotated[Optional[bool], assign_state_property]
    screening_reason: Annotated[Optional[str], assign_state_property]

    # scoring 评分阶段
    score_result: Annotated[Optional[AgentCandidateScoreSchema], assign_state_property]

    # email_invite 阶段
    email_sent: Annotated[Optional[bool], assign_state_property]
    email_sent_at: Annotated[Optional[datetime], assign_state_property]

    # negotiation 面试时间协商阶段
    negotiation_round: Annotated[int, assign_state_property]
    candidate_email_reply: Annotated[Optional[str], assign_state_property]
    proposed_interview_time: Annotated[Optional[datetime], assign_state_property]

    # manual_decision 人工闸门阶段
    manual_decision: Annotated[Optional[str], assign_state_property]

    # confirm 确认阶段
    confirmed_interview_time: Annotated[Optional[datetime], assign_state_property]
    interview_created: Annotated[Optional[bool], assign_state_property]

    # === 流程控制字段 ===
    current_stage: Annotated[
        Literal[
            "screening", "scoring", "manual_decision", "email_invite",
            "check_availability", "email_reply", "negotiation", "confirmed",
            "rejected", "refused", "negotiation_expired", "archived",
            "settled", "error"
        ],
        assign_state_property
    ]
    # 是否跳过评分直接进入邮件（人工干预场景）
    skip_scoring: Annotated[Optional[bool], assign_state_property]

    # === 可配置阈值（从 settings 注入，可在建图时覆盖） ===
    score_pass_threshold: Annotated[Optional[int], assign_state_property]
    max_negotiation_rounds: Annotated[Optional[int], assign_state_property]

    # === 错误信息（节点异常时写入） ===
    error_occurred: Annotated[Optional[bool], assign_state_property]
    error_message: Annotated[Optional[str], assign_state_property]
    error_stage: Annotated[Optional[str], assign_state_property]  # 记录异常发生时的阶段
