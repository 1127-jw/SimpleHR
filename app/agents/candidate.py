from langchain_core.tools import tool
from langchain.agents import create_agent, AgentState
from langchain.agents.middleware import ModelFallbackMiddleware, SummarizationMiddleware
from langchain_core.messages import BaseMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.message import add_messages
from langchain_core.prompts import PromptTemplate
from typing import Annotated, List, TypedDict, TypeVar, Optional, NotRequired, cast

from langgraph.prebuilt import ToolRuntime
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.llms import qwen_llm, deepseek_llm
from app.agents.prompts import CANDIDATE_PROCESS_SYSTEM_PROMPT, SCORE_FOR_CANDIDATE_SYSTEM_PROMPT, \
    SCORE_FOR_CANDIDATE_USER_PROMPT
from app.schemas.agent_schema import AgentCandidateScoreSchema
from app.schemas.candidate_schema import CandidateSchema
from app.schemas.position_schema import PositionSchema
from app.schemas.user_schema import UserSH
from app.core.config import get_settings
from app.core.db import AsyncSessionFactory
from app.repository.resume_repo import CandidateAIScoreRepo, CandidateRepo
from models.candidate import CandidateStatusEnum

settings = get_settings()

T = TypeVar("T")

def assign_state_property(left: T, right: Optional[T]) -> T:
    return right if right else left

class CandidateAgentState(AgentState):
    candidate: Annotated[CandidateSchema,assign_state_property]
    position: Annotated[PositionSchema,assign_state_property]
    interviewer: Annotated[UserSH,assign_state_property]


@tool
async def score_for_candidate(
        runtime:ToolRuntime[CandidateAgentState],
):
    """
    结合给定的候选人信息和招聘的职位要求信息，
    对候选人的工作经验、技术技能、项目经验、软技能潜力、教育背景等进行评分(最低分0分，满分10分)，
    并给出总分(综合评分)和优缺点总结
    """


    candidate: CandidateSchema = runtime.state["candidate"]
    position: PositionSchema = runtime.state["position"]

    score_agent = create_agent(
        model=qwen_llm,
        middleware=[
            ModelFallbackMiddleware(
                first_model=deepseek_llm,
            ),
        ],
        system_prompt=SCORE_FOR_CANDIDATE_SYSTEM_PROMPT,
        response_format=AgentCandidateScoreSchema
    )

    score_template = PromptTemplate.from_template(SCORE_FOR_CANDIDATE_USER_PROMPT)
    prompt = score_template.format(position=position.model_dump_json(), candidate=candidate.model_dump_json())

    res = await score_agent.ainvoke(
        prompt
    )
    candidate_score:AgentCandidateScoreSchema = res["structured_response"]
    #将候选人得分插入到数据库中
    async with AsyncSessionFactory() as session:
        async with session.begin():
            #将得分插入数据库中
            try:
                score_repo = CandidateAIScoreRepo(session)
                await score_repo.create_candidate_score(
                    candidate_id=candidate.id,
                    candidate_score_dict=candidate_score.model_dump(),
                )
                # 判断是否符合要求（总分>8）,将候选人状态修改为AI_PASS,否则为AI_FAILED
                candidate_repo = CandidateRepo(session)
                if candidate_score.overall_score > 8:
                    await candidate_repo.update_candidate_status(candidate_id=candidate.id,
                                                                 status=CandidateStatusEnum.AI_FILTER_PASSED)
                else:
                    await candidate_repo.update_candidate_status(candidate_id=candidate.id,
                                                                 status=CandidateStatusEnum.AI_FILTER_FAILED)
            except Exception as e:
                return f"ai评分写入db失败，错误信息为{e}"
    return f"ai评分工具执行成功，该候选人的评分为{candidate_score.model_dump_json()}"

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
            tools=[score_for_candidate],
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
