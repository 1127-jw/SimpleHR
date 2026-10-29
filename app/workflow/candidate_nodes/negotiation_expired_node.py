from app.workflow.candidate_state import CandidateRecruitmentState
from app.core.db import AsyncSessionFactory
from app.repository.resume_repo import CandidateRepo
from app.models.candidate import CandidateStatusEnum


async def negotiation_expired_node(state: CandidateRecruitmentState) -> dict:
    """协商超时/超过协商轮次上限 → 更新候选人状态为协商超时"""
    async with AsyncSessionFactory() as session:
        async with session.begin():
            repo = CandidateRepo(session)
            await repo.update_candidate_status(
                candidate_id=state["candidate"].id,
                status=CandidateStatusEnum.NEGOTIATION_EXPIRED,
            )
    return {"current_stage": "negotiation_expired"}
