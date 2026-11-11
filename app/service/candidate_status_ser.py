"""候选人状态流转校验与人工重试触发。

设计说明
--------
1. **状态流转校验**：从线性列表改为图状转移表，支持「失败后人工重试」的回跳。
2. **人工重试触发**：当 HR/管理员将状态改为 ``MANUAL_PASSED`` 时，
   通过 LangGraph 的 ``Command(goto=...)`` 跳转到 ``email_invite`` 节点，
   重新发送面试邀请邮件并进入协商流程。
"""
import logging

from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import AsyncSessionFactory
from app.core.exceptions import StatusFlowError
from app.models.candidate import CandidateStatusEnum
from app.repository.resume_repo import CandidateRepo
from app.repository.Interview_repo import InterviewRepo
from app.schemas.candidate_schema import CandidateSchema
from app.schemas.position_schema import PositionSchema
from app.schemas.user_schema import UserSH
from app.core.checkpointer import get_checkpointer
from app.workflow.candidate_graph import build_candidate_graph

settings = get_settings()
logger = logging.getLogger(__name__)


# ==================== 状态流转表 ====================

# 允许的状态转移：key 是源状态，value 是允许的目标状态集合
ALLOWED_TRANSITIONS: dict[CandidateStatusEnum, set[CandidateStatusEnum]] = {
    # 初始状态
    CandidateStatusEnum.APPLICATION: {
        CandidateStatusEnum.AI_FILTER_FAILED,
        CandidateStatusEnum.AI_FILTER_PASSED,
    },
    # AI 筛选失败后：可以人工通过/重试
    CandidateStatusEnum.AI_FILTER_FAILED: {
        CandidateStatusEnum.MANUAL_PASSED,
    },
    # AI 筛选成功后：正常流转
    CandidateStatusEnum.AI_FILTER_PASSED: {
        CandidateStatusEnum.WAITING_FOR_INTERVIEW,
    },
    # 协商超时后：可以人工通过/重试
    CandidateStatusEnum.NEGOTIATION_EXPIRED: {
        CandidateStatusEnum.MANUAL_PASSED,
    },
    # 处理异常后：可以人工通过/重试
    CandidateStatusEnum.PROCESSING_ERROR: {
        CandidateStatusEnum.MANUAL_PASSED,
    },
    # 人工通过/重试后：等待协商结果
    CandidateStatusEnum.MANUAL_PASSED: {
        CandidateStatusEnum.WAITING_FOR_INTERVIEW,
    },
    # 待面试后：面试结果
    CandidateStatusEnum.WAITING_FOR_INTERVIEW: {
        CandidateStatusEnum.INTERVIEW_PASSED,
        CandidateStatusEnum.INTERVIEW_REJECTED,
        CandidateStatusEnum.REFUSED_INTERVIEW,
    },
    # 面试通过后
    CandidateStatusEnum.INTERVIEW_PASSED: {
        CandidateStatusEnum.HIRED,
        CandidateStatusEnum.REJECTED,
    },
    # 面试未通过后
    CandidateStatusEnum.INTERVIEW_REJECTED: {
        CandidateStatusEnum.HIRED,
        CandidateStatusEnum.REJECTED,
    },
    # 终态（不再流转）
    CandidateStatusEnum.HIRED: set(),
    CandidateStatusEnum.REJECTED: set(),
    CandidateStatusEnum.REFUSED_INTERVIEW: set(),
}

# 允许发起人工重试的源状态
MANUAL_RETRY_SOURCES = {
    CandidateStatusEnum.AI_FILTER_FAILED,
    CandidateStatusEnum.NEGOTIATION_EXPIRED,
    CandidateStatusEnum.PROCESSING_ERROR,
}


def is_transition_allowed(
    current: CandidateStatusEnum,
    target: CandidateStatusEnum,
) -> bool:
    """检查状态流转是否合法。"""
    allowed = ALLOWED_TRANSITIONS.get(current, set())
    return target in allowed


def validate_transition(
    current: CandidateStatusEnum,
    target: CandidateStatusEnum,
) -> None:
    """校验状态流转，不合法时抛出 StatusFlowError。"""
    if not is_transition_allowed(current, target):
        raise StatusFlowError(
            f"不允许的状态流转：{current.value} → {target.value}"
        )


# ==================== 人工重试触发 ====================

async def trigger_manual_retry(candidate_id: str) -> None:
    """触发人工重试：跳转到 email_invite 节点，重新发送邀请邮件并协商。

    Args:
        candidate_id: 候选人 ID。

    注意：
        - 保留旧检查点（不调用 adelete_thread），让 LangGraph 从指定节点继续
        - 使用 Command(goto="email_invite") 跳转到邮件邀请节点
        - 清理错误状态，重置协商计数
    """
    # 1. 加载候选人完整信息
    async with AsyncSessionFactory() as session:
        async with session.begin():
            candidate_repo = CandidateRepo(session)
            candidate = await candidate_repo.get_by_id(candidate_id)
            if candidate is None:
                raise StatusFlowError(f"候选人 {candidate_id} 不存在")

            candidate_schema = CandidateSchema.model_validate(candidate)
            position_schema = PositionSchema.model_validate(candidate.position)
            interviewer_schema = UserSH.model_validate(candidate.position.creator)

    # 2. 保留旧检查点，直接跳转到 email_invite 节点
    candidate_graph = build_candidate_graph(get_checkpointer())

    await candidate_graph.ainvoke(
        Command(
            goto="email_invite",
            update={
                # 更新候选人/职位/面试官信息（防止检查点中的旧数据）
                "candidate": candidate_schema,
                "position": position_schema,
                "interviewer": interviewer_schema,
                # 清理错误状态
                "error_occurred": False,
                "error_message": None,
                "error_stage": None,
                # 重置协商状态
                "negotiation_round": 0,
                "candidate_email_reply": None,
                "proposed_interview_time": None,
                "email_sent": False,
                "email_sent_at": None,
                # 设置新阶段
                "current_stage": "email_invite",
                # 标记人工触发
                "skip_scoring": True,
            },
        ),
        config={"configurable": {"thread_id": candidate_schema.email}},
    )

    logger.info(
        "[manual_retry] candidate_id=%s | 已触发人工重试，跳转至 email_invite",
        candidate_id,
    )
