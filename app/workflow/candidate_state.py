from datetime import datetime
from typing import List, Optional, Literal, Annotated
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage
from app.schemas.candidate_schema import CandidateSchema
from app.schemas.position_schema import PositionSchema
from app.schemas.user_schema import UserSH
from app.schemas.agent_schema import AgentCandidateScoreSchema


# === 流程常量 ===
SCORE_PASS_THRESHOLD = 7          # 评分通过阈值（> 该值视为通过，与旧 score_for_candidate 保持一致）
MAX_NEGOTIATION_ROUNDS = 3        # 面试时间协商轮次上限
EMAIL_REPLY_TIMEOUT = "TIMEOUT"   # interrupt 超时恢复标记（外部调度器超时后 resume 用该值）


assign_state_property = lambda x, y: y if y is not None else x


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

    # confirm 确认阶段
    confirmed_interview_time: Annotated[Optional[datetime], assign_state_property]
    interview_created: Annotated[Optional[bool], assign_state_property]

    # === 流程控制字段 ===
    current_stage: Annotated[
        Literal[
            "screening", "scoring", "email_invite", "check_availability",
            "negotiation", "confirmed", "rejected", "refused", "negotiation_expired"
        ],
        assign_state_property
    ]
    # 是否跳过评分直接进入邮件（人工干预场景）
    skip_scoring: Annotated[Optional[bool], assign_state_property]
