"""人工闸门节点：AI 评分通过后、发邀请邮件前，等 HR 决定要不要发。

单独成文件：图里每个节点一个 ``*.py``。
"""
from __future__ import annotations

import logging

from langgraph.types import interrupt

from app.core.config import get_settings
from app.core.db import AsyncSessionFactory
from app.models.candidate import CandidateStatusEnum
from app.repository.resume_repo import CandidateRepo
from app.workflow.candidate_state import (
    MANUAL_DECISION_APPROVE,
    MANUAL_DECISION_INTERRUPT,
    MANUAL_DECISION_REJECT,
    CandidateRecruitmentState,
)

settings = get_settings()
logger = logging.getLogger(settings.NODE_LOGGER_NAME)


async def _set_status(candidate_id: str, status: CandidateStatusEnum) -> None:
    """更新候选人状态（闸门自身不抛异常：状态写失败也要让人工作业继续）。"""
    try:
        async with AsyncSessionFactory() as session:
            async with session.begin():
                await CandidateRepo(session).update_candidate_status(
                    candidate_id=candidate_id, status=status,
                )
    except Exception:  # noqa: BLE001 - 闸门不能因为状态写失败而卡死
        logger.error(
            "[manual_decision] 更新状态失败 | candidate_id=%s | status=%s",
            candidate_id, status, exc_info=True,
        )


async def manual_decision_node(state: CandidateRecruitmentState) -> dict:
    """人工闸门：评分通过后暂停，等 HR 决定要不要发面试邀请邮件。

    **不要**给这个节点加 ``@node_error_wrapper``：它靠 ``interrupt()`` 抛出的
    ``GraphInterrupt`` 让图暂停，包装器的「未知异常」分支会把 ``GraphInterrupt``
    吞成 ``AppException``，图就永远不会暂停（同 ``get_candidate_email_reply_node``）。

    恢复方式：``POST /candidate/{id}/manual-decision`` → ``Command(resume=...)``。
    """
    candidate = state["candidate"]
    cid = candidate.id

    # 先置为「待人工确认」，让候选人在列表里可识别、操作菜单才有的放矢。
    #
    # 注意：LangGraph 在 resume 时会**从头重跑本节点**（interrupt() 返回 resume 值），
    # 所以这行会执行两次。写的是同一个值，幂等无害——这也是刻意不把别的副作用
    # 放在 interrupt() 之前的原因。
    await _set_status(cid, CandidateStatusEnum.PENDING_MANUAL_DECISION)

    logger.info("[manual_decision] 挂起，等待人工决定 | candidate_id=%s", cid)
    decision = interrupt({
        "node": MANUAL_DECISION_INTERRUPT,
        "candidate_id": cid,
        "question": "是否发送面试邀请邮件？",
    })

    if decision == MANUAL_DECISION_APPROVE:
        logger.info("[manual_decision] 人工确认发送 | candidate_id=%s", cid)
        # 回到「AI 筛选成功」，让后续 confirm 正常落「待面试」
        await _set_status(cid, CandidateStatusEnum.AI_FILTER_PASSED)
        return {
            "manual_decision": MANUAL_DECISION_APPROVE,
            "current_stage": "email_invite",
        }

    logger.info(
        "[manual_decision] 人工决定不发送 | candidate_id=%s | decision=%s", cid, decision,
    )
    # 淘汰状态由 rejected_node 落 AI_FILTER_FAILED，这里不重复写
    return {
        "manual_decision": MANUAL_DECISION_REJECT,
        "current_stage": "rejected",
    }
