"""归档节点：图流程终止（淘汰 / 拒绝 / 协商超时 / 异常收尾）。

动作：追加链尾记录（``next_id = '-1'``）+ 写 ``candidates.archived_at``。
**不覆盖** ``candidates.status``——细分终态留给 HR 事后标注。

单独成文件：图里每个节点一个 ``*.py``。
"""
from __future__ import annotations

import logging

from app.core.config import get_settings
from app.core.db import AsyncSessionFactory
from app.core.exceptions import InterviewSchedulingError
from app.repository.candidate_history_repo import CandidateStatusHistoryRepo
from app.repository.resume_repo import CandidateRepo
from app.workflow.candidate_nodes.terminal_records import (
    mark_candidate_archived,
    write_terminal_record,
)
from app.workflow.candidate_state import CandidateRecruitmentState

settings = get_settings()
logger = logging.getLogger(settings.NODE_LOGGER_NAME)


async def _load_current_status(cid: str) -> str:
    """读库里该候选人**当前**的细分状态。
    """
    async with AsyncSessionFactory() as session:
        row = await CandidateRepo(session).get_by_id(cid)
        if row is None:
            raise InterviewSchedulingError(f"候选人 {cid} 不存在，无法归档")
        return str(row.status)


async def archive_node(state: CandidateRecruitmentState) -> dict:
    """归档收尾。
    """
    candidate = state["candidate"]
    position = state.get("position")
    cid = candidate.id

    if not position:
        raise InterviewSchedulingError(f"候选人 {cid} 缺少职位信息，无法归档")

    status = await _load_current_status(cid)
    logger.info("[archive] start | candidate_id=%s | status=%s", cid, status)

    await write_terminal_record(
        candidate, position, to_status=status, next_id="-1"
    )
    await mark_candidate_archived(cid)

    logger.info("[archive] done | candidate_id=%s", cid)
    return {"current_stage": "archived"}
