"""错误终结点节点。

从 ``error_handler.py`` 拆出来：图里每个节点一个 ``*.py``；
``error_handler`` 只保留 ``node_error_wrapper`` 这个所有节点共用的装饰器。
"""
from __future__ import annotations

import logging

from app.core.config import get_settings
from app.core.db import AsyncSessionFactory
from app.models.candidate import CandidateStatusEnum
from app.repository.resume_repo import CandidateRepo
from app.workflow.candidate_nodes.terminal_records import record_node_transition
from app.workflow.candidate_state import CandidateRecruitmentState

settings = get_settings()
logger = logging.getLogger(settings.NODE_LOGGER_NAME)


async def error_node(state: CandidateRecruitmentState) -> dict:
    """错误终结点节点。

    当任意节点返回 ``current_stage="error"`` 时流转至此。
    执行：

    1. 记录错误日志
    2. 更新候选人状态为 ``PROCESSING_ERROR``
    """
    candidate = state["candidate"]
    cid = candidate.id
    error_msg = state.get("error_message", "未知错误")
    error_stage = state.get("error_stage", "unknown")

    logger.error(
        "[error_node] candidate_id=%s(%s) | stage=%s | error=%s",
        cid, candidate.name, error_stage, error_msg,
    )

    try:
        async with AsyncSessionFactory() as session:
            async with session.begin():
                repo = CandidateRepo(session)
                await repo.update_candidate_status(
                    candidate_id=cid,
                    status=CandidateStatusEnum.PROCESSING_ERROR,
                )
                # 保存异常信息
                await repo.update_error_message(
                    candidate_id=cid,
                    error_message=f"[{error_stage}] {error_msg}",
                )
        # 状态变更链（与 status 同一语义；放在事务外避免 error_message 回滚影响）
        await record_node_transition(
            candidate, state.get("position"),
            CandidateStatusEnum.PROCESSING_ERROR,
        )
    except Exception as e:
        logger.critical(
            "[error_node] failed to update status | candidate_id=%s",
            cid, exc_info=True,
        )

    return {"current_stage": "error"}
