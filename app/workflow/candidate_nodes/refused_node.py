"""拒绝节点：候选人明确拒绝面试 → 更新候选人状态。"""
import logging

from app.core.config import get_settings
from app.core.db import AsyncSessionFactory
from app.models.candidate import CandidateStatusEnum
from app.repository.resume_repo import CandidateRepo
from app.workflow.candidate_state import CandidateRecruitmentState
from app.workflow.candidate_nodes.terminal_records import record_node_transition
from app.workflow.error_handler import node_error_wrapper

settings = get_settings()
logger = logging.getLogger(settings.NODE_LOGGER_NAME)


@node_error_wrapper
async def refused_node(state: CandidateRecruitmentState) -> dict:
    """候选人拒绝面试 → 更新状态。"""
    cid = state["candidate"].id
    logger.info("[refused] start | candidate_id=%s", cid)

    # 状态 + 状态变更链一起写
    await record_node_transition(
        state["candidate"], state["position"],
        CandidateStatusEnum.REFUSED_INTERVIEW,
    )

    logger.info("[refused] status updated | candidate_id=%s", cid)
    return {"current_stage": "refused"}
