from app.agents.candidate import CandidateProcessAgent
from app.schemas.candidate_schema import CandidateSchema
from app.schemas.position_schema import PositionSchema
from app.schemas.user_schema import UserSH
from loguru import logger

async def run_candidate_agent(
    candidate: CandidateSchema,
    position: PositionSchema,
    interviewer:UserSH
):
    async with CandidateProcessAgent(
        candidate=candidate,
        position=position,
    interviewer=interviewer) as agent:
        res = await agent.ainvoke(messages=[
                    {
                        "role":"user",
                        "content":f"候选人信息：{candidate.model_dump_json()},职位信息：{position.model_dump_json()}"
                    }
                ],
            thread_id=candidate.email
        )
        logger.debug(res)
        return res


