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
from app.core.exceptions import (
    DingTalkServiceError,
    EmailSendError,
    InterviewSchedulingError,
    LLMServiceError,
)
from app.models import DingdingUserModel
from app.models.candidate import CandidateStatusEnum
from app.models.InterviewModel import InterviewResultEnum
from app.core.redis_client import get_redis
from app.service.dingtalk_ser import get_user_access_token, get_calendar_list, create_calendar
from app.repository.user_repo import UserRepo
from app.repository.Interview_repo import InterviewRepo
from app.repository.resume_repo import CandidateAIScoreRepo, CandidateRepo


import logging
from datetime import datetime, timedelta
import json

logger = logging.getLogger(__name__)


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
        runtime: ToolRuntime[CandidateAgentState],
):
    """
    结合给定的候选人信息和招聘的职位要求信息，
    对候选人的工作经验、技术技能、项目经验、软技能潜力、教育背景等进行评分(最低分0分，满分10分)，
    并给出总分(综合评分)和优缺点总结。

    阈值从 state 中的 ``score_pass_threshold`` 读取。
    """

    candidate: CandidateSchema = runtime.state["candidate"]
    position: PositionSchema = runtime.state["position"]
    threshold = runtime.state.get("score_pass_threshold", 7)

    # 每次调用创建独立 Agent（原设计，保持不变）
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
    candidate_schema = CandidateSchema.model_validate(candidate)
    position_schema = PositionSchema.model_validate(position)
    prompt = score_template.format(
        position=position_schema.model_dump_json(),
        candidate=candidate_schema.model_dump_json(),
    )

    try:
        res = await score_agent.ainvoke(
            {"messages": [{"role": "user", "content": prompt}]}
        )
    except Exception as e:
        raise LLMServiceError(f"AI 评分模型调用失败: {e}") from e

    candidate_score: AgentCandidateScoreSchema = res["structured_response"]

    # 将评分结果写入数据库
    async with AsyncSessionFactory() as session:
        async with session.begin():
            score_repo = CandidateAIScoreRepo(session)
            await score_repo.create_candidate_score(
                candidate_id=candidate.id,
                candidate_score_dict=candidate_score.model_dump(),
            )
            candidate_repo = CandidateRepo(session)
            status = (
                CandidateStatusEnum.AI_FILTER_PASSED
                if candidate_score.overall_score > threshold
                else CandidateStatusEnum.AI_FILTER_FAILED
            )
            await candidate_repo.update_candidate_status(
                candidate_id=candidate.id, status=status
            )

    return (
        f"AI 评分完成，总分 {candidate_score.overall_score}，"
        f"{'通过' if candidate_score.overall_score > threshold else '未通过'}"
    )


class DingTalkTokenError(DingTalkServiceError):
    """钉钉 token 获取失败。"""
    pass


async def get_access_token(
    interviewer: UserSH,
) -> tuple[str, dict]:
    """获取面试官钉钉账户的 access token。

    Returns:
        (union_id, access_token_info) 元组。

    Raises:
        DingTalkTokenError: 未绑定钉钉账号或 token 获取失败。
    """
    async with AsyncSessionFactory() as session:
        async with session.begin():
            user_repo = UserRepo(session)
            dingding_user: DingdingUserModel | None = await user_repo.get_dingding_user(
                user_id=interviewer.id
            )
            if not dingding_user:
                raise DingTalkTokenError(
                    f"用户 {interviewer.id} 未绑定钉钉账号"
                )
            union_id = dingding_user.union_id

    try:
        r = await get_redis()
        access_token_info = await get_user_access_token(interviewer.id, r)
    except Exception as e:
        logger.error("获取钉钉 access_token 失败: %s", e)
        raise DingTalkTokenError(f"获取钉钉 access_token 失败: {e}") from e

    return union_id, access_token_info


@tool
async def get_interviewer_available_slot(
        runtime: ToolRuntime[CandidateAgentState],
):
    """
    根据职位信息，获取面试官可用的面试时间。
    """
    interviewer: UserSH = runtime.state['interviewer']
    union_id, access_token_info = await get_access_token(interviewer)

    now = now_beijing()
    tomorrow_nine = datetime(
        year=now.year, month=now.month, day=now.day, hour=9
    ) + timedelta(days=1)

    try:
        events: list[dict[str, Any]] = await get_calendar_list(
            union_id=cast(str, union_id),
            access_token=cast(str, access_token_info.get("access_token")),
            time_min=tomorrow_nine,
            time_max=tomorrow_nine + timedelta(days=7),
        )
        busy_slots = [
            (
                iso8601_to_datetime_beijing(event['start']['dateTime']),
                iso8601_to_datetime_beijing(event['end']['dateTime']),
            )
            for event in events
        ]
        available_slots: list[tuple[datetime, datetime]] = find_available_slot(
            busy_slots, start_date=tomorrow_nine
        )
    except DingTalkServiceError:
        raise
    except Exception as e:
        logger.error("获取面试官日历失败: %s", e)
        raise DingTalkServiceError(f"获取面试官日历失败: {e}") from e

    if not available_slots:
        raise InterviewSchedulingError("面试官 7 天内无空闲时间")

    available_times = [
        (slot[0].strftime("%Y-%m-%d %H:%M"), slot[1].strftime("%Y-%m-%d %H:%M"))
        for slot in available_slots
    ]
    return f"找到面试官可用的时间：{json.dumps(available_times, ensure_ascii=False)}"


@tool
async def send_interview_email(
    runtime: ToolRuntime[CandidateAgentState],
    interview_datetime_str: str,
):
    """
    给候选人发送面试时间邀请（并非最终面试时间，后续可能还需要通过邮件来和候选人协商最终面试时间）。

    Args:
        interview_datetime_str: 面试时间的字符串。
    """
    candidate = runtime.state['candidate']
    position = runtime.state['position']

    from utils.emailbot.mail_manager import get_mail_bot_
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
        logger.error("发送面试邀请邮件失败: %s", e)
        raise EmailSendError(f"发送面试邀请邮件失败: {e}") from e

    return f"面试邀请邮件已发送，初步面试时间：{interview_datetime_str}"


@tool
async def confirm_interview_time(
    interview_datetime_str: str,
    runtime: ToolRuntime[CandidateAgentState],
):
    """
    确认最终的面试时间。依次执行：
    1. 给候选人发送面试确认邮件
    2. 给面试官创建钉钉日程
    3. 在系统中创建面试预约记录
    4. 修改候选人状态为待面试

    Args:
        interview_datetime_str: 面试时间，ISO8601 格式字符串。
    """
    position = runtime.state['position']
    candidate = runtime.state['candidate']
    interviewer = runtime.state['interviewer']

    try:
        interview_datetime: datetime = iso8601_to_datetime_beijing(interview_datetime_str)
        interview_datetime_without_tz = to_naive_beijing(interview_datetime)
    except Exception as e:
        raise InterviewSchedulingError(
            f"面试时间格式化失败: {interview_datetime_str}"
        ) from e

    # 1. 发送确认邮件
    subject = "【知了课堂】面试时间确定"
    body = f"""
"尊敬的{candidate.name}，
面试时间已确定：
{interview_datetime_str}
请您准时参加面试。该邮件无需再回复。谢谢！
"""
    from utils.emailbot.mail_manager import get_mail_bot_
    email_bot = get_mail_bot_()
    try:
        await email_bot.send_email(
            to=candidate.email,
            subject=subject,
            text=body,
        )
    except Exception as e:
        logger.error("发送面试确认邮件失败: %s", e)
        raise EmailSendError(f"发送面试确认邮件失败: {e}") from e

    # 2. 获取钉钉 token
    union_id, access_token_info = await get_access_token(interviewer)
    access_token = access_token_info.get("access_token")

    # 3. 创建钉钉日程
    try:
        end_datetime: datetime = interview_datetime + timedelta(hours=1)
        await create_calendar(
            union_id=union_id,
            access_token=access_token,
            summary=f"面试安排：{position.title} - {candidate.name}",
            start_datetime=interview_datetime,
            end_datetime=end_datetime,
        )
    except DingTalkServiceError:
        raise
    except Exception as e:
        logger.error("创建钉钉日程失败: %s", e)
        raise DingTalkServiceError(f"创建钉钉日程失败: {e}") from e

    # 4. 创建面试记录 + 更新候选人状态
    try:
        async with AsyncSessionFactory() as session:
            async with session.begin():
                interview_repo = InterviewRepo(session)
                await interview_repo.create_interview({
                    "scheduled_time": interview_datetime_without_tz,
                    "result": InterviewResultEnum.PENDING,
                    "candidate_id": candidate.id,
                    "interviewer_id": interviewer.id,
                })
                candidate_repo = CandidateRepo(session)
                await candidate_repo.update_candidate_status(
                    candidate_id=candidate.id,
                    status=CandidateStatusEnum.WAITING_FOR_INTERVIEW,
                )
    except Exception as e:
        logger.error("创建面试记录失败: %s", e)
        raise InterviewSchedulingError(
            f"创建面试记录失败: {e}"
        ) from e

    return (
        "面试时间确认完成：已发送确认邮件、"
        "创建钉钉日程、创建面试记录、更新候选人状态"
    )


@tool
async def refuse_interview(
    runtime: ToolRuntime[CandidateAgentState],
):
    """
    候选人拒绝面试时调用，更新候选人状态为拒绝面试。

    Raises:
        InterviewSchedulingError: 更新候选人状态失败。
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
    except Exception as e:
        logger.error("更新候选人拒绝状态失败: %s", e)
        raise InterviewSchedulingError(
            f"更新候选人 {candidate.id} 拒绝状态失败: {e}"
        ) from e
    return "已更新候选人状态为拒绝面试"


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
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        serde = JsonPlusSerializer(
            allowed_msgpack_modules=[
                ("app.models.position", "EducationEnum"),
                ("app.models.user", "UserStatus"),
                ("app.models.candidate", "GenderEnum"),
                ("app.models.candidate", "CandidateStatusEnum"),
                ("app.models.InterviewModel", "InterviewResultEnum"),
                ("app.schemas.position_schema", "PositionSchema"),
                ("app.schemas.candidate_schema", "CandidateSchema"),
                ("app.schemas.user_schema", "UserSH"),
                ("app.schemas.agent_schema", "AgentCandidateScoreSchema"),
            ],
        )
        self._checkpointer_conn = AsyncPostgresSaver.from_conn_string(pg_url, serde=serde)
        self._checkpointer = await self._checkpointer_conn.__aenter__()
        await self._checkpointer.setup()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._checkpointer_conn.__aexit__(exc_type, exc_val, exc_tb)
