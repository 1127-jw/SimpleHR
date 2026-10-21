from langchain_core.tools import tool
from langchain.agents import create_agent, AgentState
from langchain.agents.middleware import ModelFallbackMiddleware, SummarizationMiddleware
from langchain_core.messages import BaseMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.message import add_messages
from langchain_core.prompts import PromptTemplate
from typing import Annotated, List, TypedDict, TypeVar, Optional, NotRequired, cast, Any

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
from app.models import DingdingUserModel
from app.models.candidate import CandidateStatusEnum
from app.core.redis_client import get_redis
from app.repository.user_repo import UserRepo
from app.service.dingtalk_ser import get_user_access_token, get_calendar_list

from loguru import logger
from datetime import datetime, timedelta
import json

from utils.time_ISO8601_datatime import iso8601_to_datetime_beijing
from utils.available_time import find_available_slot
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


@tool
async def get_interviewer_available_slot(
        runtime:ToolRuntime[CandidateAgentState],
):
    """
       根据职位信息，获取面试官可用的面试时间。
       """
    interviewer: UserSH = runtime.state['interviewer']
    # 获取该用户的钉钉账号
    union_id: str|None = None
    async with AsyncSessionFactory() as session:
        async with session.begin():
            try:
                user_repo = UserRepo(session)
                dingding_user: DingdingUserModel | None = await user_repo.get_dingding_user(user_id=interviewer.id)
                if not dingding_user:
                    return f"获取候选人可用时间失败，没有绑定钉钉账号！"
                union_id = dingding_user.union_id
            except Exception as e:
                return f"获取候选人可用时间失败：{e}"
    try:
        # 2. 获取access_token
        r = await get_redis()
        access_token_info = await get_user_access_token(interviewer.id, r)
    except Exception as e:
        logger.error(e)
        return f"获取access_toke败：{e}"

    # 3. 从钉钉上获取面试官的日程安排
    try:
        now = datetime.now()
        tomorrow_nine = datetime(year=now.year, month=now.month, day=now.day, hour=9) + timedelta(days=1)
        events: list[dict[str, Any]] = await get_calendar_list(
            union_id=cast(str, union_id),
            access_token=cast(str, access_token_info.get("access_token")),
            time_min=tomorrow_nine,
            time_max=tomorrow_nine + timedelta(days=7),
        )
        busy_slots = [
            (iso8601_to_datetime_beijing(event['start']['dateTime']), iso8601_to_datetime_beijing(event['end']['dateTime']))
            for event in events
        ]
        available_slots: list[tuple[datetime, datetime]] = find_available_slot(
            busy_slots,
            start_date=tomorrow_nine
        )
        if len(available_slots) == 0:
            return f"获取面试官可用时间失败：7天内没有空闲时间！"
        available_times = [(slot[0], slot[1]) for slot in available_slots]
        return f"找到面试官可用的时间：{json.dumps(available_times)}"

    except Exception as e:
        return f"获取面试官可用时间失败：{e}"


class CandidateProcessAgent:
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
