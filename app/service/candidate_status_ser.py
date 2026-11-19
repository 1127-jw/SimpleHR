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
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import AsyncSessionFactory
from app.core.exceptions import StatusFlowError
from app.models.candidate import CandidateModel, CandidateStatusEnum
from app.repository.candidate_history_repo import CandidateStatusHistoryRepo
from app.repository.resume_repo import CandidateRepo
from app.repository.Interview_repo import InterviewRepo
from app.schemas.candidate_schema import CandidateSchema
from app.schemas.position_schema import PositionSchema
from app.schemas.user_schema import UserSH
from app.core.checkpointer import get_checkpointer
from app.core.time import db_now
from app.workflow.candidate_graph import build_candidate_graph
from app.workflow.candidate_state import (
    MANUAL_DECISION_APPROVE,
    MANUAL_DECISION_ARCHIVE,
    MANUAL_DECISION_INTERRUPT,
    MANUAL_DECISION_REJECT,
    is_waiting_on,
    thread_id_of,
)

settings = get_settings()
logger = logging.getLogger(__name__)


# ==================== 状态流转表 ====================

# 允许的状态转移：key 是源状态，value 是允许的目标状态集合
ALLOWED_TRANSITIONS: dict[CandidateStatusEnum, set[CandidateStatusEnum]] = {
    # 初始状态
    CandidateStatusEnum.APPLICATION: {
        CandidateStatusEnum.AI_FILTER_FAILED,
        # 评分达标后直接进人工确认（AI筛选成功不再是需要人工再点一次的中间态）
        CandidateStatusEnum.PENDING_MANUAL_DECISION,
    },
    # AI 筛选失败后：人工重试，或（归档后）补标细分终态
    CandidateStatusEnum.AI_FILTER_FAILED: {
        CandidateStatusEnum.MANUAL_PASSED,
        CandidateStatusEnum.REJECTED,
        CandidateStatusEnum.HIRED,
    },
    # AI 筛选成功：保留给历史数据 / 统计；新流程不再产生该状态
    # （评分达标直接进 PENDING_MANUAL_DECISION）
    CandidateStatusEnum.AI_FILTER_PASSED: {
        CandidateStatusEnum.PENDING_MANUAL_DECISION,
        CandidateStatusEnum.WAITING_FOR_INTERVIEW,
    },
    # 待人工确认：确认发送（进入协商）、淘汰，或归档（保持当前状态）
    CandidateStatusEnum.PENDING_MANUAL_DECISION: {
        CandidateStatusEnum.NEGOTIATING,
        CandidateStatusEnum.AI_FILTER_FAILED,
    },
    # 面试时间协商中：候选人拒绝 / 轮次耗尽 / 回复超时
    CandidateStatusEnum.NEGOTIATING: {
        CandidateStatusEnum.REFUSED_INTERVIEW,
        CandidateStatusEnum.NEGOTIATION_EXPIRED,
        CandidateStatusEnum.WAITING_FOR_INTERVIEW,
    },
    # 归档后允许 HR 补标细分终态（归档信息在 candidates.archived_at）
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

async def mark_candidate_error(candidate_id: str, error_msg: str) -> None:
    """将候选人状态标记为 ``PROCESSING_ERROR``，并保存异常信息。

    供所有「非 HTTP 请求链路」的调用方复用：
    ``run_candidate_agent`` / ``notify`` / 超时巡检 / 邮件投递。
    即使状态更新本身失败也只做 ``critical`` 日志，绝不向上抛。
    """
    try:
        async with AsyncSessionFactory() as session:
            async with session.begin():
                repo = CandidateRepo(session)
                await repo.update_candidate_status(
                    candidate_id=candidate_id,
                    status=CandidateStatusEnum.PROCESSING_ERROR,
                )
                await repo.update_error_message(
                    candidate_id=candidate_id,
                    error_message=error_msg,
                )
        logger.info("[mark_error] 候选人 %s 已标记为处理异常", candidate_id)
    except Exception as e:  # noqa: BLE001 - 兜底清理失败不能阻断调用方
        logger.critical(
            "[mark_error] 标记候选人 %s 处理异常状态失败: %s",
            candidate_id, e, exc_info=True,
        )


async def _load_graph_models(candidate_id: str):
    """加载图所需的 candidate / position / interviewer 三个 schema。

    两种人工触发（``trigger_manual_retry`` 的 goto、``resume_manual_decision``
    的 resume）都要用同一份，避免各自读库读到不一致的快照。
    """
    async with AsyncSessionFactory() as session:
        async with session.begin():
            candidate = await CandidateRepo(session).get_by_id(candidate_id)
            if candidate is None:
                raise StatusFlowError(f"候选人 {candidate_id} 不存在")
            return (
                CandidateSchema.model_validate(candidate),
                PositionSchema.model_validate(candidate.position),
                UserSH.model_validate(candidate.position.creator),
            )


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
    candidate_schema, position_schema, interviewer_schema = await _load_graph_models(
        candidate_id
    )

    # 2. 写链：旧链终止 + 开新链（这是「归档后重新发起」的落地方式）
    async with AsyncSessionFactory() as session:
        async with session.begin():
            await CandidateStatusHistoryRepo(session).start_new_chain(
                candidate_id,
                to_status=CandidateStatusEnum.NEGOTIATING.value,
                from_status=str(candidate_schema.status),
                candidate_email=candidate_schema.email,
                position_id=position_schema.id,
                position_title=position_schema.title,
            )
            # 重新发起即「未归档」：不清掉的话，归档筛选会把这条正在进行的流程
            # 当成已归档（archived_at 还是上一轮的时间）。
            await session.execute(
                update(CandidateModel)
                .where(CandidateModel.id == candidate_id)
                .values(archived_at=None)
            )

    # 3. 保留旧检查点，直接跳转到 email_invite 节点
    candidate_graph = build_candidate_graph(get_checkpointer())

    from app.core.limiters import task_slot

    # 与启动流程 / 候选人回复共用 "recruit" 名额
    async with task_slot("recruit", settings.RECRUIT_TASK_MAX_CONCURRENCY):
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
                    # skip_scoring 必须为真。实测 ``goto="email_invite"`` 在这条路径上
                    # **仍会先经过 screening / scoring 节点**（日志里能看到 route_screening）：
                    # 不跳过的后果有两个——重复插入 AI 评分（撞
                    # ix_candidate_ai_scores_candidate_id），以及 scoring 走正常分支后
                    # 再次发邀请 / 再进一次人工闸门。
                    "skip_scoring": True,
                },
            ),
            config={"configurable": {"thread_id": thread_id_of(candidate_id)}},
        )

    logger.info(
        "[manual_retry] candidate_id=%s | 已触发人工重试，跳转至 email_invite",
        candidate_id,
    )


async def resume_manual_decision(candidate_id: str, decision: str) -> None:
    """人工闸门：把决定 resume 回停在 ``manual_decision`` 的状态图。

    与 ``trigger_manual_retry`` 的区别：那个是 ``Command(goto=...)`` 另起一段流程；
    这个是 ``Command(resume=...)`` **唤醒已经暂停的 interrupt**，
    检查点里评分阶段的完整状态原样保留。

    Args:
        candidate_id: 候选人 ID。
        decision: ``MANUAL_DECISION_APPROVE`` / ``MANUAL_DECISION_REJECT``
            / ``MANUAL_DECISION_ARCHIVE``。

    Raises:
        StatusFlowError: 候选人不存在，或当前并不停在人工闸门（防止误 resume）。
    """
    if decision not in (
        MANUAL_DECISION_APPROVE,
        MANUAL_DECISION_REJECT,
        MANUAL_DECISION_ARCHIVE,
    ):
        raise StatusFlowError(f"未知的人工决定: {decision!r}")

    candidate_schema, position_schema, interviewer_schema = await _load_graph_models(
        candidate_id
    )
    thread_id = thread_id_of(candidate_id)

    candidate_graph = build_candidate_graph(get_checkpointer())

    # 两道校验，缺一不可：
    # 1）中断点身份——只认「人工闸门」，避免被别人的 resume 值误开；
    # 2）库里的状态——状态与检查点不一致时（例如枚举迁移前写的旧值）以库为准。
    snapshot = await candidate_graph.aget_state({"configurable": {"thread_id": thread_id}})
    if not is_waiting_on(snapshot.interrupts, MANUAL_DECISION_INTERRUPT):
        raise StatusFlowError(f"候选人 {candidate_id} 当前不在待人工确认状态")
    if candidate_schema.status != CandidateStatusEnum.PENDING_MANUAL_DECISION:
        raise StatusFlowError(
            f"候选人 {candidate_id} 状态为 {candidate_schema.status}，不是待人工确认"
        )

    # 归档决定：链尾直接置 '-1'，不必再走图（图侧 archive_node 也会写，
    # 但那条路径要求 interrupt 被 resume；这里已经确定要归档，就地收口更直接）。
    if decision == MANUAL_DECISION_ARCHIVE:
        async with AsyncSessionFactory() as session:
            async with session.begin():
                await CandidateStatusHistoryRepo(session).mark_archived(
                    candidate_id,
                    to_status=str(candidate_schema.status),
                    from_status=str(candidate_schema.status),
                    candidate_email=candidate_schema.email,
                    position_id=position_schema.id,
                    position_title=position_schema.title,
                )
                from sqlalchemy import update

                from app.models.candidate import CandidateModel

                await session.execute(
                    update(CandidateModel)
                    .where(CandidateModel.id == candidate_id)
                    .values(archived_at=db_now())
                )
        logger.info("[manual_decision] candidate_id=%s | archive | 已就地归档", candidate_id)
        return

    from app.core.limiters import task_slot

    # 与启动流程 / 候选人回复共用 "recruit" 名额
    async with task_slot("recruit", settings.RECRUIT_TASK_MAX_CONCURRENCY):
        await candidate_graph.ainvoke(
            Command(
                resume=decision,
                update={
                    # 刷新候选人/职位/面试官信息（防止检查点中的旧数据）
                    "candidate": candidate_schema,
                    "position": position_schema,
                    "interviewer": interviewer_schema,
                },
            ),
            config={"configurable": {"thread_id": thread_id}},
        )

    logger.info(
        "[manual_decision] candidate_id=%s | decision=%s | 已唤醒流程",
        candidate_id, decision,
    )
