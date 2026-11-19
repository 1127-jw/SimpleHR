import logging
from datetime import datetime, timedelta, time

from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.runnables import RunnableWithFallbacks

from app.agents.llms import get_deepseek_llm, get_qwen_llm
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
from app.core.time import from_naive_beijing, now_beijing

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
    primary = get_deepseek_llm().with_structured_output(ProposedTime)
    fallback = get_qwen_llm().with_structured_output(ProposedTime)
    return RunnableWithFallbacks(runnable=primary, fallbacks=[fallback])


# ==================== 检查函数 ====================
def check_hours(available_slots, proposed_dt, duration, time_window=None):
    for start, end in available_slots:
        if start <= proposed_dt and proposed_dt + duration <= end:
            return True
    return False


def check_am_or_pm(available_slots, proposed_dt, duration, time_window: tuple[time, time]):
    win_start, win_end = time_window
    # 必须带上 proposed_dt 的时区：钉钉空闲时段是 aware datetime，
    # datetime.combine 默认产出 naive，直接比较会抛
    # "can't compare offset-naive and offset-aware datetimes"。
    window_start = datetime.combine(proposed_dt, win_start, tzinfo=proposed_dt.tzinfo)
    window_end = datetime.combine(proposed_dt, win_end, tzinfo=proposed_dt.tzinfo)
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


# ==================== 把模糊提议解析成具体面试时间 ====================
def resolve_interview_slot(
    strategy_name: str,
    available_slots,
    proposed_dt: datetime,
    duration: timedelta,
    time_window: tuple[time, time] | None = None,
) -> datetime | None:
    """由 LLM 的模糊提议解析出一个**可以直接排进日历**的具体起始时间。
    * ``hours``：候选人给了准确时间，原样使用（前提是落在某段空闲区间内）；
    * ``am`` / ``pm``：在对应时间窗里取第一个够长的空闲段的**起点**；
    * ``all``：当天任意够长的空闲段，取起点。

    Returns:
        可用的起始时间；没有任何满足条件的空闲段时返回 ``None``。
    """
    if strategy_name == "hours":
        for start, end in available_slots:
            if start <= proposed_dt and proposed_dt + duration <= end:
                return proposed_dt
        return None

    if strategy_name == "all":
        for start, end in available_slots:
            if end - start >= duration:
                return start
        return None

    if time_window is None:
        raise NegotiationError(f"策略 {strategy_name} 缺少时间窗配置")

    win_start, win_end = time_window
    # 窗口必须带上 proposed_dt 的时区：钉钉空闲时段是 aware datetime，
    # datetime.combine 默认产出 naive，直接比较会抛 TypeError。
    window_start = datetime.combine(proposed_dt, win_start, tzinfo=proposed_dt.tzinfo)
    window_end = datetime.combine(proposed_dt, win_end, tzinfo=proposed_dt.tzinfo)
    for start, end in available_slots:
        if window_start <= start and end <= window_end and end - start >= duration:
            return start
    return None


# ==================== 提议时间解析 ====================
# 提示词要求 "YYYY-MM-DD HH:MM:SS"，但模型偶尔会给出 "T" 分隔、省略秒、
# 或补上小数秒/时区偏移。这里做归一化，避免一条格式偏差就白白消耗一轮协商。
_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
)


def parse_proposed_time(raw: str) -> datetime:
    """把模型输出的时间字符串解析成 aware 北京时间。

    Raises:
        NegotiationError: 所有格式都匹配不上。
    """
    text = (raw or "").strip()
    if not text:
        raise NegotiationError("候选人提议的时间为空")
    text = text.replace("T", " ")
    # 去掉小数秒与 "+08:00" 之类的偏移：日期部分本身已是北京时间字面量
    text = text.split("+")[0].split("Z")[0].strip()
    if "." in text:
        text = text.split(".")[0]
    for fmt in _TIME_FORMATS:
        try:
            return from_naive_beijing(datetime.strptime(text, fmt))
        except ValueError:
            continue
    raise NegotiationError(f"无法解析候选人提议的时间: {raw!r}")


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

    from app.core.limiters import llm_slot

    async with llm_slot("llm:time_parse"):
        result: ProposedTime = await llm.ainvoke(messages)
    logger.info(
        "[check_avail] LLM parsed | confirm=%s | refused=%s | proposed_time=%s | exactly=%s",
        result.confirm, result.refused, result.proposed_time, result.exactly,
    )
    return result


async def _notify_candidate_slot_unavailable(candidate, proposed_dt: datetime) -> None:
    """best-effort 回信：告知提议时间不可用并请候选人另选。
    发信失败只记日志、不改变流程推进 —— 回信是体验优化，不能因为它把
    协商轮次卡住。
    """
    try:
        from utils.emailbot.mail_manager import get_mail_bot_

        email_bot = get_mail_bot_()
        if not email_bot.is_connected:
            await email_bot.connect()
        body = f"""
尊敬的{candidate.name}，
您好！
感谢您的回复。您提出的面试时间（{proposed_dt.strftime('%Y-%m-%d %H:%M')}）面试官暂时无法安排。
麻烦您再提供一到两个方便的时间，我们会尽快确认。
谢谢！
"""
        await email_bot.send_email(
            to=candidate.email,
            subject="面试时间协商-该时段暂不可用",
            text=body,
        )
        logger.info(
            "[check_avail] 已回信告知时段不可用 | candidate_id=%s", candidate.id
        )
    except Exception:  # noqa: BLE001 - 回信属于增强项，不能拖垮协商推进
        logger.warning(
            "[check_avail] 回信告知时段不可用失败（不影响流程推进） | candidate_id=%s",
            getattr(candidate, "id", None), exc_info=True,
        )


async def can_not_find_available_time(
    state: CandidateRecruitmentState,
    *,
    notify_candidate: bool = False,
    proposed_dt: datetime | None = None,
):
    """本轮协商失败：递增轮次。

    Args:
        notify_candidate: 是否给候选人回信。只有「候选人明确提出时间但不可用」
            时才该回信；空回复 / 未提供时间时发信没有意义。
        proposed_dt: 被拒绝的具体时间，用于回信正文。
    """
    candidate = state["candidate"]
    cid = candidate.id
    round_num = state.get("negotiation_round", 0) + 1
    logger.info(
        "[check_avail] cannot find available time | candidate_id=%s | round=%d",
        cid, round_num,
    )

    if notify_candidate and proposed_dt is not None:
        await _notify_candidate_slot_unavailable(candidate, proposed_dt)

    return {
        "current_stage": "negotiation",
        "negotiation_round": round_num,
    }


# 单场面试时长：同时作为空闲时段的切分粒度（hour_interval），
# 否则 check_hours 只能命中整点小时的时段，10:45 这类提议会被误判为不可用。
INTERVIEW_DURATION = timedelta(minutes=30)


@node_error_wrapper
async def check_availability_node(state: CandidateRecruitmentState) -> dict:
    """检查候选人提议时间是否与面试官空闲时间冲突。

    节点内直接分流「拒绝」和「同意原时间」，其余情况才查钉钉空闲时段比对；
    「未提供时间」走 can_not_find_available_time 递增协商轮次。
    """
    candidate = state["candidate"]
    interviewer = state["interviewer"]
    cid = candidate.id
    reply = state.get("candidate_email_reply", "")

    logger.info("[check_avail] start | candidate_id=%s", cid)

    # 只处理「已提供具体时间」的场景
    if not reply:
        # 走 can_not_find_available_time 而不是直接返回：路由会原样回到邮件回复节点，
        # 不递增 negotiation_round 就永远碰不到轮次上限，协商只能靠 48h 超时巡检收尾。
        return await can_not_find_available_time(state)

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

    # 候选人同意原时间：state["proposed_interview_time"] 是首次邀请时从面试官空闲时段
    # 里挑出来的，无需再查一次钉钉，直接按「可用」放行进 confirm 分支。
    # 缺这个分支时「可以，就这个时间」会因 proposed_time 为空被判成协商失败，
    # 耗尽轮次后走到 negotiation_expired。
    if pt.confirm:
        if not state.get("proposed_interview_time"):
            # 首次邀请时「7 天内无空闲时段」那条分支只发了「能否改到 7 天后」的
            # 通知，并没有确定任何具体时间。此时候选人回一句「可以」，若原样放行，
            # confirm_node 读 state["proposed_interview_time"] 会抛 KeyError，
            # 整条流程落进 error。这种情况必须回到协商，而不是确认。
            logger.info(
                "[check_avail] candidate agreed but no proposed time in state "
                "| candidate_id=%s", cid,
            )
            return await can_not_find_available_time(state)
        logger.info("[check_avail] candidate accepted original time | candidate_id=%s", cid)
        return {
            "candidate_email_reply": REPLY_TIME_AVAILABLE,
            "current_stage": "check_availability",
        }

    # 未提供时间：回复里没有可用的时间信息，递增协商轮次后继续协商
    if not pt.proposed_time:
        logger.info("[check_avail] no time proposed | candidate_id=%s", cid)
        return await can_not_find_available_time(state)

    # 2. 解析时间：回复里的时间是北京时间字面量，补上时区后才能和
    #    钉钉空闲时段（aware）比较
    try:
        proposed_dt = parse_proposed_time(pt.proposed_time)
    except NegotiationError as e:
        logger.error("[check_avail] invalid time format | candidate_id=%s | error=%s", cid, e)
        return await can_not_find_available_time(state)

    # 3. 获取面试官空闲时间
    #    get_user_available_slots 自己按 interviewer_id 解析 union_id / access_token，
    #    这里只传「查哪一天、切多细」：从提议日的 00:00 起只查 1 天，
    #    使日历查询窗口与空闲时段计算窗口对齐（否则会漏掉当天更早的忙碌日程）。
    try:
        available_slots = await get_user_available_slots(
            interviewer_id=interviewer.id,
            hour_interval=INTERVIEW_DURATION.total_seconds() / 3600,
            max_days_to_search=1,
            start_time=proposed_dt.replace(hour=0, minute=0, second=0, microsecond=0),
            time_delta=0,
        )
    except DingTalkServiceError as e:
        logger.error("[check_avail] get_available_slots failed | candidate_id=%s | error=%s", cid, e)
        raise

    if not available_slots:
        logger.info("[check_avail] no slots found | candidate_id=%s", cid)
        return await can_not_find_available_time(state)

    # 4. 解析出可以直接排进日历的具体时间
    #    「上午/下午/全天」的 proposed_dt 是当日 00:00（仅用于携带日期），
    #    直接拿它当面试时间会把面试排到零点，必须由窗口内的空闲段反推真实起点。
    #    exactly 为空时按「精确时间」处理：模型给了 proposed_time 却漏填分类，
    #    比直接抛 NegotiationError 更合理。
    strategy_name = str(pt.exactly or "hours")
    if strategy_name not in CHECK_STRATEGIES:
        raise NegotiationError(
            f"未知面试时间策略: {pt.exactly}，"
            f"支持的策略: {list(CHECK_STRATEGIES.keys())}"
        )
    win_time = DAILY_WINDOWS.get(strategy_name, None)

    interview_start = resolve_interview_slot(
        strategy_name, available_slots, proposed_dt, INTERVIEW_DURATION, win_time
    )

    if interview_start is None:
        logger.info(
            "[check_avail] slot not free | candidate_id=%s | exactly=%s | proposed=%s",
            cid, pt.exactly, proposed_dt,
        )
        return await can_not_find_available_time(
            state, notify_candidate=True, proposed_dt=proposed_dt
        )

    # 5. 时间可用，返回具体起始时间（而不是 proposed_dt）
    logger.info(
        "[check_avail] slot available | candidate_id=%s | exactly=%s | start=%s",
        cid, pt.exactly, interview_start,
    )
    return {
        "candidate_email_reply": REPLY_TIME_AVAILABLE,
        "proposed_interview_time": interview_start,
        "current_stage": "negotiation",
    }
