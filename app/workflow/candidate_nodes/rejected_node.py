"""淘汰节点：筛选 / 评分未通过 → 更新候选人状态。"""
import logging

from app.core.config import get_settings
from app.core.db import AsyncSessionFactory
from app.models.candidate import CandidateStatusEnum
from app.repository.resume_repo import CandidateRepo
from app.workflow.candidate_state import CandidateRecruitmentState
from app.workflow.error_handler import node_error_wrapper

settings = get_settings()
logger = logging.getLogger(settings.NODE_LOGGER_NAME)


@node_error_wrapper
async def rejected_node(state: CandidateRecruitmentState) -> dict:
    """筛选/评分淘汰 → 更新状态。"""
    cid = state["candidate"].id
    logger.info("[rejected] start | candidate_id=%s", cid)

    async with AsyncSessionFactory() as session:
        async with session.begin():
            repo = CandidateRepo(session)
            await repo.update_candidate_status(
                candidate_id=cid, status=CandidateStatusEnum.AI_FILTER_FAILED,
            )

    logger.info("[rejected] status updated | candidate_id=%s", cid)
    return {"current_stage": "rejected"}
