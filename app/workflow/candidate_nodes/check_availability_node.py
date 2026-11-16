import logging
from datetime import datetime, timedelta, time

from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.runnables import RunnableWithFallbacks
from langgraph.types import interrupt

from app.agents.llms import deepseek_llm, qwen_llm
from app.agents.prompts import PROPOSED_TIME_SYSTEM_PROMPT
from app.core.config import get_settings
from app.core.exceptions import (
    DingTalkServiceError,
    EmailSendError,
    NegotiationError,
)
from app.schemas.intent_schema import ProposedTime
from app.service.dingtalk_ser import get_user_available_slots
from app.workflow.candidate_state import (
    CandidateRecruitmentState,
    REPLY_REFUSED,
    REPLY_TIME_AVAILABLE,
)
from app.workflow.error_handler import node_error_wrapper
from utils.time_ISO8601_datatime import BEIJING_TZ, now_beijing

settings = get_settings()
logger = logging.getLogger(settings.NODE_LOGGER_NAME)

# ==================== 时间文本生成（替代 get_current_time 工具）====================
def _build_current_time_text() -> str:
    """生成当前北京时间文本，直接注入 Prompt，替代原 get_current_time 工具。"""
    now_bj = now_beijing()
    weekday_map = {
        0: "星期一", 1: "星期二", 2: "星期三", 3: "星期四",
        4: "星期五", 5: "星期六", 6: "星期日",
    }
    weekday_cn = weekday_map[now_bj.weekday()]
    return (
        f"{now_bj.year}年{now_bj.month}月{now_bj.day}日 "
        f"{now_bj.hour:02d}:{now_bj.minute:02d}:{now_bj.second:02d} "
        f"{weekday_cn}（本月第{now_bj.day}天）"
    )


def _build_time_llm() -> RunnableWithFallbacks:
    """构建带 fallback 的结构化时间解析 LLM。"""
    primary = deepseek_llm.with_structured_output(ProposedTime)
    fallback = qwen_llm.with_structured_output(ProposedTime)
    return RunnableWithFallbacks(runnable=primary, fallbacks=[fallback])


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
    "all": check_day,
}

DAILY_WINDOWS = {
    "am": (time(9, 0), time(12, 0)),
    "pm": (time(14, 0), time(18, 0)),
}


async def _extract_time_from_reply(state: CandidateRecruitmentState, reply: str) -> ProposedTime:
    """从邮件正文中提取结构化面试意向（直接 LLM 调用，时间注入 Prompt）。

    原实现通过 ``create_agent`` + ``get_current_time`` 工具让模型自查时间；
    这里改为把当前时间直接拼进 System Prompt，省掉一次工具往返。
    """
    current_time_text = _build_current_time_text()
    system_prompt = (
        f"{PROPOSED_TIME_SYSTEM_PROMPT}\n\n"
        f"【当前时间】{current_time_text}\n"
        f"（请直接以上述当前时间为基准推算相对日期，无需调用任何工具）"
    )

    llm = _build_time_llm()
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=f"候选人邮件回复内容：\n{reply}"),
    ]

    result: ProposedTime = await llm.ainvoke(messages)
    logger.info(
        "[check_avail] LLM parsed | confirm=%s | refused=%s | proposed_time=%s | exactly=%s",
        result.confirm, result.refused, result.proposed_time, result.exactly,
    )
    return result


async def get_candidate_email_reply_node(state: CandidateRecruitmentState):
    """中断节点：等待候选人邮件回复。"""
    cid = state["candidate"].id
    logger.info("[email_reply] waiting for candidate reply | candidate_id=%s", cid)
    logger.info(
        "[email_reply] resume thread before interrupt | candidate_id=%s | thread_id=%s",
        cid, state["candidate"].email,
    )
    try:
        reply = interrupt(value=None)
    except Exception as e:
        logger.error("解析候选人邮件回复失败: %s", e)
        reply = ""
    if reply is None:
        reply = ""

    return {
        "candidate_email_reply": reply,
        "current_stage": "email_reply",
    }


async def can_not_find_available_time(state: CandidateRecruitmentState):
    candidate = state["candidate"]
    cid = candidate.id
    round_num = state.get("negotiation_round", 0) + 1
    logger.info(
        "[check_avail] cannot find available time | candidate_id=%s | round=%d",
        cid, round_num,
    )
    return {
        "current_stage": "negotiation",
        "negotiation_round": round_num,
        "last_proposed_time": None,
    }


@node_error_wrapper
async def check_availability_node(state: CandidateRecruitmentState) -> dict:
    """检查候选人提议时间是否与面试官空闲时间冲突。

    只处理「已提供具体时间」的场景；同意/拒绝/未提供时间的情况由路由函数直接分流。
    """
    from utils.emailbot.mail_manager import get_mail_bot_

    candidate = state["candidate"]
    interviewer = state["interviewer"]
    cid = candidate.id
    reply = state.get("candidate_email_reply", "")

    logger.info("[check_avail] start | candidate_id=%s", cid)

    # 只处理「已提供具体时间」的场景
    if not reply:
        return {"current_stage": "negotiation"}

    # 1. LLM 提取结构化时间（时间已注入 Prompt，无需工具）
    try:
        pt = await _extract_time_from_reply(state, reply)
    except Exception as e:
        logger.error("[check_avail] LLM parse failed | candidate_id=%s | error=%s", cid, e)
        return await can_not_find_available_time(state)

    # 明确拒绝
    if pt.refused:
        logger.info("[check_avail] candidate refused | candidate_id=%s", cid)
        return {
            "candidate_email_reply": REPLY_REFUSED,
            "current_stage": "negotiation",
        }

    # 未提供时间
    if not pt.proposed_time:
        logger.info("[check_avail] no time proposed | candidate_id=%s", cid)
        return await can_not_find_available_time(state)

    # 2. 解析时间
    try:
        proposed_dt = datetime.strptime(pt.proposed_time, "%Y-%m-%d %H:%M:%S")
        proposed_dt = proposed_dt.replace(tzinfo=BEIJING_TZ)
    except ValueError as e:
        logger.error("[check_avail] invalid time format | candidate_id=%s | error=%s", cid, e)
        return await can_not_find_available_time(state)

    # 3. 获取面试官空闲时间
    try:
        available_slots = await get_user_available_slots(
            access_token=interviewer.access_token,
            union_id=interviewer.union_id,
            start_time=proposed_dt,
            duration=timedelta(minutes=30),
        )
    except DingTalkServiceError as e:
        logger.error("[check_avail] get_available_slots failed | candidate_id=%s | error=%s", cid, e)
        raise

    if not available_slots:
        logger.info("[check_avail] no slots found | candidate_id=%s", cid)
        return await can_not_find_available_time(state)

    # 4. 检查时间是否可用
    duration = timedelta(minutes=30)
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

    # 5. 时间可用，返回结果
    logger.info("[check_avail] slot available | candidate_id=%s | time=%s", cid, proposed_dt)
    return {
        "candidate_email_reply": "SYSTEM_TIME_AVAILABLE",
        "proposed_interview_time": proposed_dt,
        "current_stage": "negotiation",
    }
