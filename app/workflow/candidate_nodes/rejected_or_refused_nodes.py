import logging
from app.workflow.candidate_state import CandidateRecruitmentState
from app.workflow.error_handler import node_error_wrapper
from app.core.db import AsyncSessionFactory
from app.repository.resume_repo import CandidateRepo
from app.models.candidate import CandidateStatusEnum

logger = logging.getLogger(__name__)


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


@node_error_wrapper
async def refused_node(state: CandidateRecruitmentState) -> dict:
    """候选人拒绝面试 → 更新状态。"""
    cid = state["candidate"].id
    logger.info("[refused] start | candidate_id=%s", cid)

    async with AsyncSessionFactory() as session:
        async with session.begin():
            repo = CandidateRepo(session)
            await repo.update_candidate_status(
                candidate_id=cid, status=CandidateStatusEnum.REFUSED_INTERVIEW,
            )

    logger.info("[refused] status updated | candidate_id=%s", cid)
    return {"current_stage": "refused"}
