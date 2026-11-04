from langchain.agents.middleware import ModelFallbackMiddleware
from langchain_core.tools import tool
from langgraph.types import interrupt
from langchain.agents import create_agent
import logging

logger = logging.getLogger(__name__)

from app.agents.prompts import PROPOSED_TIME_SYSTEM_PROMPT
from app.core.config import get_settings
from app.core.exceptions import (
    DingTalkServiceError,
    EmailSendError,
    NegotiationError,
)
from app.workflow.candidate_state import CandidateRecruitmentState
from app.workflow.error_handler import node_error_wrapper
from app.service.dingtalk_ser import get_user_available_slots
from datetime import datetime, timedelta, time

from app.agents.llms import deepseek_llm, qwen_llm
from pydantic import BaseModel, Field

from utils.time_ISO8601_datatime import BEIJING_TZ, now_beijing

settings = get_settings()


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

# 面试时间窗口（从 Settings 读取，可配置）
DAILY_WINDOWS = {
    "am": settings.am_window,
    "pm": settings.pm_window,
}


# ==================== 节点函数 ====================
def get_candidate_email_reply_node(state: CandidateRecruitmentState) -> dict:
    """
    中断点：等待候选人回复邮件。
    图执行到此节点后暂停，直到外部调用 resume。
    """
    cid = state["candidate"].id
    logger.info("[email_reply] waiting for candidate reply | candidate_id=%s", cid)

    candidate_email_reply = interrupt("candidate_email_reply")

    logger.info(
        "[email_reply] resumed | candidate_id=%s | reply=%.100s",
        cid, str(candidate_email_reply),
    )
    return {
        "candidate_email_reply": candidate_email_reply,
        "current_stage": "check_availability",
    }


async def _extract_time_from_reply(text: str) -> ProposedTime:
    """从候选人邮件回复中提取候选人是否同意上次邀约的时间，若不同意则提取提议的面试时间"""
    try:
        result = await _proposed_time_agent.ainvoke(
            {"messages": [{"role": "user", "content": text}]}
        )
        return result["structured_response"]
    except Exception as e:
        logger.error("解析候选人邮件回复失败: %s", e)
        raise NegotiationError(f"解析候选人邮件回复失败: {e}") from e


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
    raise NegotiationError(
        f"无法解析时间格式: {time_str}，支持的格式: {formats}"
    )


async def can_not_find_available_time(state: CandidateRecruitmentState) -> dict:
    """
    面试官时间不可用时发送通知邮件。
    不区分"无空闲"和"时间冲突"——都让候选人提供另外的时间。
    """
    candidate = state["candidate"]
    cid = candidate.id
    round_num = state.get("negotiation_round", 0) + 1

    logger.info(
        "[check_avail] interviewer not available | "
        "candidate_id=%s | round=%d | sending notice",
        cid, round_num,
    )

    from utils.emailbot.mail_manager import get_mail_bot_
    email_bot = get_mail_bot_()
    subject = "面试时间调整"
    body = "您提出的时间面试官暂不可用，请选择其他具体时间。"

    try:
        if not email_bot.is_connected:
            logger.warning(
                "[check_avail] email not connected, reconnecting | candidate_id=%s",
                cid,
            )
            await email_bot.connect()
        await email_bot.send_email(
            to=candidate.email,
            subject=subject,
            text=body,
        )
    except EmailSendError:
        raise
    except Exception as e:
        logger.error("[check_avail] failed to send notice | candidate_id=%s | error=%s", cid, e)
        raise EmailSendError(f"通知候选人重新选择时间失败: {e}") from e

    logger.info("[check_avail] notice sent | candidate_id=%s | round=%d", cid, round_num)

    return {
        "candidate_email_reply": "SYSTEM_TIME_UNAVAILABLE",
        "current_stage": "negotiation",
        "negotiation_round": state.get("negotiation_round", 0) + 1,
    }


@node_error_wrapper
async def check_availability_node(state: CandidateRecruitmentState) -> dict:
    """
    验证候选人提议的时间面试官是否空闲。
    """
    candidate = state["candidate"]
    cid = candidate.id

    logger.info("[check_avail] start | candidate_id=%s", cid)

    # 1. 从候选人回复中提取提议的时间
    try:
        pt = await _extract_time_from_reply(state["candidate_email_reply"])
    except NegotiationError:
        raise

    # 候选人拒绝面试
    if pt.refused:
        logger.info("[check_avail] candidate refused | candidate_id=%s", cid)
        return {
            "candidate_email_reply": "CANDIDATE_REFUSED",
            "proposed_interview_time": state["proposed_interview_time"],
            "current_stage": "refused",
        }

    # 候选人确认面试时间
    if pt.confirm:
        logger.info("[check_avail] candidate confirmed | candidate_id=%s", cid)
        return {
            "candidate_email_reply": "SYSTEM_TIME_AVAILABLE",
            "proposed_interview_time": state["proposed_interview_time"],
            "current_stage": "confirmed",
        }

    # 候选人提议新时间 → 检查面试官是否空闲
    # 若候选人未给出具体时间（仅说"换个时间"），视为时间不可用，回退等待
    if not pt.proposed_time:
        logger.info("[check_avail] no time proposed | candidate_id=%s", cid)
        return await can_not_find_available_time(state)
    proposed_dt = await _parse_proposed_time(pt.proposed_time)
    duration = timedelta(hours=1)

    logger.info(
        "[check_avail] checking availability | candidate_id=%s | proposed=%s | exactly=%s",
        cid, proposed_dt, pt.exactly,
    )

    # 2. 获取面试官可用时间段
    try:
        available_slots = await get_user_available_slots(
            interviewer_id=state["interviewer"].id,
            hour_interval=1.0,
            max_days_to_search=1,
            start_time=proposed_dt,
            time_delta=0,
        )
    except DingTalkServiceError:
        raise
    except Exception as e:
        logger.error("[check_avail] get_available_slots failed | candidate_id=%s | error=%s", cid, e)
        raise DingTalkServiceError(f"获取面试官日历失败: {e}") from e

    # 3. 无空闲时间或时间冲突 → 统一处理：通知候选人重新选择
    if not available_slots:
        logger.info("[check_avail] no slots found | candidate_id=%s", cid)
        return await can_not_find_available_time(state)

    win_time = DAILY_WINDOWS.get(pt.exactly, None)
    strategy = CHECK_STRATEGIES.get(str(pt.exactly))
    if strategy is None:
        raise NegotiationError(
            f"未知面试时间策略: {pt.exactly}，"
            f"支持的策略: {list(CHECK_STRATEGIES.keys())}"
        )
    is_free = strategy(available_slots, proposed_dt, duration, win_time)

    if not is_free:
        logger.info(
            "[check_avail] slot not free | candidate_id=%s | exactly=%s | proposed=%s",
            cid, pt.exactly, proposed_dt,
        )
        return await can_not_find_available_time(state)

    # 4. 时间可用，返回结果
    logger.info("[check_avail] slot available | candidate_id=%s | time=%s", cid, proposed_dt)
    return {
        "candidate_email_reply": "SYSTEM_TIME_AVAILABLE",
        "proposed_interview_time": proposed_dt,
        "current_stage": "negotiation",
    }
