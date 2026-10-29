from langchain_core.tools import tool
from langchain.agents import create_agent, AgentState
from langchain.agents.middleware import ModelFallbackMiddleware, SummarizationMiddleware
from langchain_core.messages import BaseMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.prebuilt import ToolRuntime
from langchain_core.prompts import PromptTemplate

from typing import Annotated, TypeVar, Optional, cast, Any

from app.agents.llms import qwen_llm, deepseek_llm
from app.agents.prompts import CANDIDATE_PROCESS_SYSTEM_PROMPT, SCORE_FOR_CANDIDATE_SYSTEM_PROMPT, \
    SCORE_FOR_CANDIDATE_USER_PROMPT
from app.schemas.agent_schema import AgentCandidateScoreSchema
from app.schemas.candidate_schema import CandidateSchema
from app.schemas.position_schema import PositionSchema
from app.schemas.user_schema import UserSH
from app.core.config import get_settings
from app.core.db import AsyncSessionFactory
from app.models import DingdingUserModel
from app.models.candidate import CandidateStatusEnum
from app.models.InterviewModel import InterviewResultEnum
from app.core.redis_client import get_redis
from app.service.dingtalk_ser import get_user_access_token, get_calendar_list, create_calendar
from app.repository.user_repo import UserRepo
from app.repository.Interview_repo import InterviewRepo
from app.repository.resume_repo import CandidateAIScoreRepo, CandidateRepo


from loguru import logger
from datetime import datetime, timedelta
import json


from utils.emailbot.mail_manager import get_mail_bot_
from utils.time_ISO8601_datatime import iso8601_to_datetime_beijing, now_beijing, to_naive_beijing
from utils.available_time import find_available_slot

# from langchain_core.globals import set_debug, set_verbose
#
# set_debug(True)
# set_verbose(False)  # 避免日志重复

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
    # 将 ORM 模型转为 Pydantic Schema，再序列化
    candidate_schema = CandidateSchema.model_validate(candidate)
    position_schema = PositionSchema.model_validate(position)
    prompt = score_template.format(position=position_schema.model_dump_json(), candidate=candidate_schema.model_dump_json())

    res = await score_agent.ainvoke(
        {"messages": [{"role": "user", "content": prompt}]}
    )
    candidate_score: AgentCandidateScoreSchema = res["structured_response"]
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
                # 判断是否符合要求（总分>7）,将候选人状态修改为AI_PASS,否则为AI_FAILED
                candidate_repo = CandidateRepo(session)
                if candidate_score.overall_score > 7:
                    await candidate_repo.update_candidate_status(candidate_id=candidate.id,
                                                                 status=CandidateStatusEnum.AI_FILTER_PASSED)
                else:
                    await candidate_repo.update_candidate_status(candidate_id=candidate.id,
                                                                 status=CandidateStatusEnum.AI_FILTER_FAILED)
            except Exception as e:
                logger.error(f"AI评分写入数据库失败: {e}")
                return f"ai评分写入db失败，错误信息为{e}"
    return f"ai评分工具执行成功，该候选人的评分为{candidate_score.model_dump_json()}"


async def get_access_token(
    interviewer:UserSH
):
    """获取面试官钉钉账户的access token"""
    async with AsyncSessionFactory() as session:
        async with session.begin():
            try:
                user_repo = UserRepo(session)
                dingding_user: DingdingUserModel | None = await user_repo.get_dingding_user(user_id=interviewer.id)
                if not dingding_user:
                    return f"获取access token 失败，没有绑定钉钉账号！"
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
    return union_id, access_token_info


@tool
async def get_interviewer_available_slot(
        runtime:ToolRuntime[CandidateAgentState],
):
    """
       根据职位信息，获取面试官可用的面试时间。
       """
    interviewer: UserSH = runtime.state['interviewer']
    # 获取该用户的钉钉账号
    result = await get_access_token(interviewer)
    # 错误时返回字符串，成功时返回元组
    if isinstance(result, str):
        return result
    union_id, access_token_info = result

    # 3. 从钉钉上获取面试官的日程安排
    try:
        now = now_beijing()
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
        available_times = [(slot[0].strftime("%Y-%m-%d %H:%M"), slot[1].strftime("%Y-%m-%d %H:%M")) for slot in available_slots]
        return f"找到面试官可用的时间：{json.dumps(available_times, ensure_ascii=False)}"

    except Exception as e:
        return f"获取面试官可用时间失败：{e}"


@tool
async def send_interview_email(
    runtime:ToolRuntime[CandidateAgentState],
    interview_datetime_str:str,
):
    """
    给候选人发送面试时间邀请（并非最终面试时间，后续可能还需要通过邮件来和候选人协商最终面试时间）
    :param interview_datetime_str: 面试时间的字符串
    """
    candidate = runtime.state['candidate']
    position = runtime.state['position']

    email_bot = get_mail_bot_()

    subject = "【知了课堂】面试邀请-协商面试时间"
    body = f"""
尊敬的{candidate.name}，
您好！
感谢您投递我司{position.title}职位。
我们初步确定了您的面试时间，请您确认是否方便。
面试时间：{interview_datetime_str}
请您确认是否方便，如果方便，请您回复“确认”。
如果不方便，请回复您方便的时间，我们将会重新协商面试时间。
谢谢！
            """
    try:
        await email_bot.send_email(
            to=candidate.email,
            subject=subject,
            text=body,
        )
    except Exception as e:
        logger.error(e)
        return f"给候选人发送邮件失败：{e}"

    return f"给候选人发送面试邀请邮件成功！面试时间初步确定为：{interview_datetime_str}"


@tool
async def confirm_interview_time(
    interview_datetime_str: str,
    runtime: ToolRuntime[CandidateAgentState],
):
    """
    确认最终的面试时间。这个工具会做以下四件事：
    * 通过邮件，发送最终确认面试的时间给候选人
    * 给面试官的钉钉创建一个面试的日程安排
    * 在系统中创建一个面试预约记录
    # 在系统中修改候选人的状态为待面试
    :param interview_datetime_str: 面试时间，ISO8601格式的字符串
    """
    position = runtime.state['position']
    candidate = runtime.state['candidate']
    interviewer = runtime.state['interviewer']

    try:
        interview_datetime: datetime = iso8601_to_datetime_beijing(interview_datetime_str)
        # 统一使用 to_naive_beijing 剥离时区（确保存入 DB 的是北京时间数值）
        interview_datetime_without_tz = to_naive_beijing(interview_datetime)
    except Exception as e:
        return f"{interview_datetime_str}格式化失败！{e}"

    subject = "【知了课堂】面试时间确定"
    body = f"""
"尊敬的{candidate.name}，
面试时间已确定：
{interview_datetime_str}
请您准时参加面试。该邮件无需再回复。谢谢！
"""
    email_bot = get_mail_bot_()
    try:
        await email_bot.send_email(
            to=candidate.email,
            subject=subject,
            text=body,
        )
    except Exception as e:
        logger.error(e)
        return f"给候选人发送邮件失败：{e}"

    result = await get_access_token(interviewer)
    # 错误时返回字符串，成功时返回元组
    if isinstance(result, str):
        return result
    union_id, access_token_info = result
    access_token = access_token_info.get("access_token")

    try:
        end_datetime: datetime = interview_datetime + timedelta(hours=1)
        await create_calendar(
            union_id=union_id,
            access_token=access_token,
            summary=f"面试安排：{position.title} - {candidate.name}",
            start_datetime=interview_datetime,
            end_datetime=end_datetime,
        )
    except Exception as e:
        return f"创建钉钉面试日程安排失败，错误信息：\n{e}"


    try:
        async with AsyncSessionFactory() as session:
            async with session.begin():
                interview_repo = InterviewRepo(session)
                # 3. 在数据库中创建一个面试预约记录
                await interview_repo.create_interview({
                    # 面试时间因为是存储到postgresql数据库中，现在是只能存储没有带时区的日期
                    "scheduled_time": interview_datetime_without_tz,
                    "result": InterviewResultEnum.PENDING,
                    "candidate_id": candidate.id,
                    "interviewer_id": interviewer.id,
                })
                # 4. 在数据库中修改候选人的状态为待面试
                candidate_repo = CandidateRepo(session)
                await candidate_repo.update_candidate_status(
                    candidate_id=candidate.id,
                    status=CandidateStatusEnum.WAITING_FOR_INTERVIEW,
                )
    except Exception as e:
        return f"在系统中创建面试预约记录和候选人状态修改失败！{e}"

    return f"""
    * 给候选人发送面试时间执行成功！
    * 给面试官创建钉钉日程安排成功！
    * 在系统中创建面试预约成功！
    * 在系统中修改候选人状态为待面试成功！
    """


@tool
async def refuse_interview(
    runtime: ToolRuntime[CandidateAgentState],
):
    """
    如果候选人拒绝了面试，那么调用该工具来更新候选人状态为拒绝面试
    """
    candidate = runtime.state['candidate']
    try:
        async with AsyncSessionFactory() as session:
            async with session.begin():
                candidate_repo = CandidateRepo(session)
                await candidate_repo.update_candidate_status(
                    candidate_id=candidate.id,
                    status=CandidateStatusEnum.REFUSED_INTERVIEW,
                )
        return "已修改候选人状态为拒绝面试！"
    except Exception as e:
        return f"修改候选人状态为拒绝面试失败！"


@tool
def get_current_time() -> str:
    """
    获取当前时间（北京时间）：年月日、时分秒、星期几、当月第几天
    """
    now_bj = now_beijing()

    weekday_map = {
        0: "星期一",
        1: "星期二",
        2: "星期三",
        3: "星期四",
        4: "星期五",
        5: "星期六",
        6: "星期日",
    }
    weekday_cn = weekday_map[now_bj.weekday()]

    day_of_month = now_bj.day

    return (
        f"{now_bj.year}年{now_bj.month}月{now_bj.day}日 "
        f"{now_bj.hour:02d}:{now_bj.minute:02d}:{now_bj.second:02d} "
        f"{weekday_cn}（本月第{day_of_month}天）"
    )


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
            tools=[
                score_for_candidate,
                get_interviewer_available_slot,
                send_interview_email,
                confirm_interview_time,
                refuse_interview,
                get_current_time
            ],
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
                "messages": messages,
                "candidate": self.candidate,
                "position": self.position,
                "interviewer": self.interviewer,
            },
            config={"configurable": {"thread_id": thread_id}},
        )
        return resp
    async def __aenter__(self):
        # AsyncPostgresSaver 需要 postgresql:// 格式，去掉 SQLAlchemy 的 +psycopg 前缀
        pg_url = settings.database_url.replace("+psycopg", "")
        self._checkpointer_conn = AsyncPostgresSaver.from_conn_string(pg_url)
        self._checkpointer = await self._checkpointer_conn.__aenter__()
        await self._checkpointer.setup()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._checkpointer_conn.__aexit__(exc_type, exc_val, exc_tb)
