"""结算节点：``confirm`` 确认面试时间后的收尾。

与归档的区别只在 ``next_id``：结算记 ``'-2'``（流程**正常完成**，
后续由 HR 手动改状态），归档记 ``'-1'``（淘汰/放弃/异常收尾）。

但两者都会写 ``candidates.archived_at``——它表达的是「**图流程已走完**」，
不是「被淘汰」。后续 HR 把状态改成「已入职 / 已拒绝 / …」是图之外的人工维护。

单独成文件：图里每个节点一个 ``*.py``。
"""
from __future__ import annotations

import logging

from app.core.config import get_settings
from app.core.exceptions import InterviewSchedulingError
from app.models.candidate import CandidateStatusEnum
from app.workflow.candidate_nodes.terminal_records import (
    mark_candidate_archived,
    write_terminal_record,
)
from app.workflow.candidate_state import CandidateRecruitmentState

settings = get_settings()
logger = logging.getLogger(settings.NODE_LOGGER_NAME)


async def settle_node(state: CandidateRecruitmentState) -> dict:
    """结算收尾。

    注意：**不能**用 ``@node_error_wrapper`` 包——理由同 ``archive_node``。
    """
    candidate = state["candidate"]
    position = state.get("position")
    cid = candidate.id

    if not position:
        raise InterviewSchedulingError(f"候选人 {cid} 缺少职位信息，无法结算")

    logger.info("[settle] start | candidate_id=%s", cid)

    await write_terminal_record(
        candidate,
        position,
        to_status=CandidateStatusEnum.WAITING_FOR_INTERVIEW.value,
        from_status=CandidateStatusEnum.NEGOTIATING.value,
        next_id="-2",
    )
    # 图流程走完 → 写归档时间（与 archive_node 一致）
    await mark_candidate_archived(cid)

    logger.info("[settle] done | candidate_id=%s", cid)
    return {"current_stage": "settled"}
