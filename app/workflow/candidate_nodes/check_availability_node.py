from langchain.agents.middleware import ModelFallbackMiddleware
from langchain_core.tools import tool
from langgraph.types import interrupt
from langchain.agents import create_agent
from loguru import logger

from app.agents.prompts import PROPOSED_TIME_SYSTEM_PROMPT
from app.workflow.candidate_state import CandidateRecruitmentState
from app.service.dingtalk_ser import get_user_available_slots
from datetime import datetime, timedelta, time

from app.agents.llms import deepseek_llm, qwen_llm
from pydantic import BaseModel, Field

from utils.emailbot.mail_manager import get_mail_bot_
from utils.time_ISO8601_datatime import BEIJING_TZ, now_beijing


# ==================== Schema ====================
class ProposedTime(BaseModel):
    confirm: bool = Field(
        description="True 表示邮件内容为同意上次提供的邀约时间，False 表示邮件不同意上次提供的邀约时间"
    )
    refused: bool = Field(
        description="True 表示邮件内容为回拒该次面试，False 表示邮件中提供了面试时间"
    )
    proposed_time: str = Field(
        description="面试时间，格式：YYYY-MM-DD HH:MM:SS"
    )
    exactly: str = Field(
        description="am/pm 表示上午/下午；如果是准确时间则为hours"
    )


# ==================== 工具 ====================
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


# ==================== 模块级单例，避免每次调用创建 Agent ====================
_proposed_time_agent = create_agent(
    model=deepseek_llm,
    system_prompt=PROPOSED_TIME_SYSTEM_PROMPT,
    middleware=[ModelFallbackMiddleware(first_model=qwen_llm)],
    tools=[get_current_time],
    response_format=ProposedTime,
)


# ==================== 检查函数 ====================
def check_hours(available_slots, proposed_dt, duration, time_window=None):
    for start, end in available_slots:
        if start <= proposed_dt and proposed_dt + duration <= end:
            return True
    return False


def check_am_or_pm(available_slots, proposed_dt, duration, time_window: tuple[time, time]):
    win_start, win_end = time_window
    window_start = datetime.combine(proposed_dt, win_start)
    window_end = datetime.combine(proposed_dt, win_end)
    for start, end in available_slots:
        if window_start <= start and end - start >= duration and end <= window_end:
            return True
    return False


def check_day(available_slots, proposed_dt, duration, time_window=None):
    for start, end in available_slots:
        if end - start >= duration:
            return True
    return False


# ==================== 策略表 ====================
CHECK_STRATEGIES = {
    "hours": check_hours,
    "am": check_am_or_pm,
    "pm": check_am_or_pm,
    "day": check_day,
}

DAILY_WINDOWS = {
    "am": (time(9, 0), time(12, 0)),
    "pm": (time(14, 0), time(18, 0)),
}


# ==================== 节点函数 ====================
def get_candidate_email_reply_node(state: CandidateRecruitmentState) -> dict:
    """
    中断点：等待候选人回复邮件。
    图执行到此节点后暂停，直到外部调用 resume。
    """
    candidate_email_reply = interrupt("candidate_email_reply")
    return {
        "candidate_email_reply": candidate_email_reply,
        "current_stage": "check_availability"
    }


async def _extract_time_from_reply(text: str) -> ProposedTime:
    """从候选人邮件回复中提取候选人是否同意上次邀约的时间，若不同意则提取提议的面试时间"""
    result = await _proposed_time_agent.ainvoke(
        {"messages": [{"role": "user", "content": text}]}
    )
    try:
        return result["structured_response"]
    except Exception as e:
        logger.error(f"解析候选人邮箱获取面试时间失败，错误信息：{e}")
        raise ValueError(f"解析候选人邮箱获取面试时间失败，错误信息：{e}")


async def _parse_proposed_time(time_str: str) -> datetime:
    """解析面试时间字符串，支持多种格式"""
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(time_str, fmt)
        except ValueError:
            continue
    raise ValueError(f"无法解析时间格式: {time_str}，支持的格式: {formats}")


async def can_not_find_available_time(state: CandidateRecruitmentState) -> dict:
    """
    面试官时间不可用时发送通知邮件。
    不区分"无空闲"和"时间冲突"——都让候选人提供另外的时间。
    """
    email_bot = get_mail_bot_()
    subject = "面试时间调整"
    body = "您提出的时间面试官暂不可用，请选择其他具体时间。"

    try:
        if not email_bot.is_connected:
            logger.warning("邮箱未连接，尝试重新连接")
            await email_bot.connect()
        await email_bot.send_email(
            to=state["candidate"].email,
            subject=subject,
            text=body,
        )
    except Exception as e:
        logger.error(f"给候选人发送邮件失败：{e}")
        raise ValueError(f"给候选人发送邮件失败：{e}")

    return {
        "candidate_email_reply": "SYSTEM_TIME_UNAVAILABLE",
        "current_stage": "negotiation",
        "negotiation_round": state.get("negotiation_round", 0) + 1,
    }


async def check_availability_node(state: CandidateRecruitmentState) -> dict:
    """
    验证候选人提议的时间面试官是否空闲。
    """

    # 1. 从候选人回复中提取提议的时间
    pt = await _extract_time_from_reply(state["candidate_email_reply"])

    # 候选人拒绝面试
    if pt.refused:
        return {
            "candidate_email_reply": "CANDIDATE_REFUSED",
            "proposed_interview_time": state["proposed_interview_time"],
            "current_stage": "refused",
        }

    # 候选人确认面试时间
    if pt.confirm:
        return {
            "candidate_email_reply": "SYSTEM_TIME_AVAILABLE",
            "proposed_interview_time": state["proposed_interview_time"],
            "current_stage": "confirmed",
        }

    # 候选人提议新时间 → 检查面试官是否空闲
    # 若候选人未给出具体时间（仅说"换个时间"），视为时间不可用，回退等待
    if not pt.proposed_time:
        return await can_not_find_available_time(state)
    proposed_dt = await _parse_proposed_time(pt.proposed_time)
    duration = timedelta(hours=1)

    # 2. 获取面试官可用时间段
    try:
        available_slots = await get_user_available_slots(
            interviewer_id=state["interviewer"].id,
            hour_interval=1.0,
            max_days_to_search=1,
            start_time=proposed_dt,
            time_delta=0,
        )
    except Exception as e:
        logger.error(f"获取面试官可用时间段失败：{e}")
        raise ValueError(f"获取面试官可用时间段失败：{e}")

    # 3. 无空闲时间或时间冲突 → 统一处理：通知候选人重新选择
    if not available_slots:
        return await can_not_find_available_time(state)

    win_time = DAILY_WINDOWS.get(pt.exactly, None)
    strategy = CHECK_STRATEGIES.get(str(pt.exactly))
    if strategy is None:
        raise ValueError(
            f"未知策略: {pt.exactly}，"
            f"支持的策略: {list(CHECK_STRATEGIES.keys())}"
        )
    is_free = strategy(available_slots, proposed_dt, duration, win_time)

    if not is_free:
        return await can_not_find_available_time(state)

    # 4. 时间可用，返回结果
    return {
        "candidate_email_reply": "SYSTEM_TIME_AVAILABLE",
        "proposed_interview_time": proposed_dt,
        "current_stage": "negotiation",
    }
