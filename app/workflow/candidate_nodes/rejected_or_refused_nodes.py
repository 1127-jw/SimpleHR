from app.workflow.candidate_state import CandidateRecruitmentState
from app.core.db import AsyncSessionFactory
from app.repository.resume_repo import CandidateRepo
from app.models.candidate import CandidateStatusEnum


async def rejected_node(state: CandidateRecruitmentState) -> dict:
    """AI 评分不达标 → 更新状态为筛选淘汰"""
    async with AsyncSessionFactory() as session:
        async with session.begin():
            repo = CandidateRepo(session)
            await repo.update_candidate_status(
                candidate_id=state["candidate"].id,
                status=CandidateStatusEnum.AI_FILTER_FAILED
            )
    return {"current_stage": "rejected"}


async def refused_node(state: CandidateRecruitmentState) -> dict:
    """候选人拒绝面试 → 更新状态为拒绝"""
    async with AsyncSessionFactory() as session:
        async with session.begin():
            repo = CandidateRepo(session)
            await repo.update_candidate_status(
                candidate_id=state["candidate"].id,
                status=CandidateStatusEnum.REFUSED_INTERVIEW
            )
    return {"current_stage": "refused"}
