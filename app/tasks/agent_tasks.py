from envs.myenv.Lib.asyncio import graph

from app.core.config import get_settings
from app.agents.candidate import CandidateProcessAgent
from app.schemas.candidate_schema import CandidateSchema
from app.schemas.position_schema import PositionSchema
from app.schemas.user_schema import UserSH
from loguru import logger

from core.checkpointer import get_checkpointer
from workflow.candidate_graph import build_candidate_graph

settings = get_settings()
async def run_candidate_agent(
    candidate: CandidateSchema,
    position: PositionSchema,
    interviewer:UserSH
):
    graph = settings.GRAPH_OPEN
    if not graph:
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
    else:
        async with get_checkpointer() as checkpointer:
            candidate_graph = build_candidate_graph(checkpointer)
            res = await candidate_graph.ainvoke({
                "candidate": candidate,
                "position": position,
                "interviewer": interviewer,
                "current_stage": "screening",
            },
                config={"configurable": {"thread_id": candidate.email}}
            )
        return res

