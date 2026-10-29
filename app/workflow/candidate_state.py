from datetime import datetime, time
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
            "negotiation", "confirmed", "rejected", "refused",
            "negotiation_expired", "error"
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
