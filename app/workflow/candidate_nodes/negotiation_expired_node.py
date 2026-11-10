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
async def negotiation_expired_node(state: CandidateRecruitmentState) -> dict:
    """协商超时/超过协商轮次上限 → 更新候选人状态为协商超时。"""
    cid = state["candidate"].id
    logger.info(
        "[negotiation_expired] start | candidate_id=%s | round=%d",
        cid, state.get("negotiation_round", 0),
    )

    async with AsyncSessionFactory() as session:
        async with session.begin():
            repo = CandidateRepo(session)
            await repo.update_candidate_status(
                candidate_id=cid, status=CandidateStatusEnum.NEGOTIATION_EXPIRED,
            )

    logger.info("[negotiation_expired] status updated | candidate_id=%s", cid)
    return {"current_stage": "negotiation_expired"}
