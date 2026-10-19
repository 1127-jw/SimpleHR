from langchain.agents import create_agent, AgentState
from langchain.agents.middleware import ModelFallbackMiddleware, SummarizationMiddleware
from langchain_core.messages import BaseMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.message import add_messages
from typing import Annotated, List, TypedDict, TypeVar, Optional, NotRequired, cast
from dataclasses import dataclass

from app.agents.llms import qwen_llm, deepseek_llm
from app.agents.prompts import CANDIDATE_PROCESS_SYSTEM_PROMPT
from app.schemas.agent_schema import AgentCandidateSchema
from app.schemas.candidate_schema import CandidateSchema
from app.schemas.position_schema import PositionSchema
from app.schemas.user_schema import UserSH
from app.core.config import get_settings

settings = get_settings()

T = TypeVar("T")

def assign_state_property(left: T, right: Optional[T]) -> T:
    return right if right else left

class CandidateAgentState(AgentState):
    candidate: Annotated[CandidateSchema,assign_state_property]
    position: Annotated[PositionSchema,assign_state_property]
    interviewer: Annotated[UserSH,assign_state_property]


class CandidateProcessAgent():
    def __init__(self,
        candidate:CandidateSchema|None,
        position:PositionSchema|None,
        interviewer:UserSH|None,
                 ):
        """

        :param candidate: 候选人信息
        :param position: 职位信息
        :param interviewer: 面试者信息
        """
        self.candidate = candidate
        self.position = position
        self.interviewer = interviewer
        self._checkpointer = None


    async def ainvoke(self,messages:list[BaseMessage],thread_id:str):
        assert self._checkpointer is not None
        agent = create_agent(
            model=qwen_llm,
            tools=[],
            system_prompt=CANDIDATE_PROCESS_SYSTEM_PROMPT,
            state_schema=cast(type[AgentState], CandidateAgentState),
            middleware=[
                ModelFallbackMiddleware(
                    first_model=deepseek_llm,
                ),
                SummarizationMiddleware(
                    model=deepseek_llm,
                    trigger=("tokens", 50000),
                    keep=("tokens", 10000),
                ),
            ],
            checkpointer=self._checkpointer,
        )
        resp = await agent.ainvoke(
            {
                "messages":messages,
            },
            thread_id=thread_id,
        )
        return resp
    async def __aenter__(self):
        self._checkpointer_conn = AsyncPostgresSaver.from_conn_string(settings.database_url)
        self._checkpointer = await self._checkpointer_conn.__aenter__()
        await self._checkpointer.setup()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._checkpointer_conn.__aexit__(exc_type, exc_val, exc_tb)
