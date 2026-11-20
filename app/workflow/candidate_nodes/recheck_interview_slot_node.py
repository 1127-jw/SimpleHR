"""复核节点：确认时间后、创建日程前，再查一次面试官该时段是否仍空闲。
"""
from __future__ import annotations

import logging
from datetime import datetime

from app.core.config import get_settings
from app.core.exceptions import (
    DingTalkServiceError,
    InterviewSchedulingError,
)
from app.service.dingtalk_ser import get_user_available_slots
from app.core.time import now_beijing
from app.workflow.candidate_nodes.check_availability_node import (
    INTERVIEW_DURATION,
    resolve_interview_slot,
)
from app.workflow.candidate_nodes.mail_notify import send_workflow_email
from app.workflow.candidate_state import (
    REPLY_SLOT_TAKEN,
    REPLY_TIME_AVAILABLE,
    CandidateRecruitmentState,
)
from app.workflow.error_handler import node_error_wrapper

settings = get_settings()
logger = logging.getLogger(settings.NODE_LOGGER_NAME)

# 候选人已经给出确切时间，按精确时间复核（不是「上午/下午」那种模糊提议）
_STRATEGY = "hours"

SLOT_TAKEN_SUBJECT = "面试时间协商-原时段暂不可用"


async def _notify_slot_taken(candidate, proposed_dt: datetime) -> None:
    """告知候选人原时段已被占用，请其重新约时间。

    这封信是**重新启动协商**的手段，不是可选通知：发不出去就等于流程卡死，
    所以失败要抛 ``EmailSendError`` 让节点进 error 状态，而不是静默跳过。
    """
    body = f"""
尊敬的{candidate.name}，
您好！
非常抱歉，您确认的面试时间（{proposed_dt.strftime('%Y-%m-%d %H:%M')}）面试官临时已有其他安排，暂时无法确认。
麻烦您重新回复一个方便的时间，我们会尽快与您确认。
谢谢！
"""
    await send_workflow_email(
        candidate.email, subject=SLOT_TAKEN_SUBJECT, text=body,
    )


@node_error_wrapper
async def recheck_interview_slot_node(state: CandidateRecruitmentState) -> dict:
    """复核面试官在 ``proposed_interview_time`` 是否仍空闲。"""
    candidate = state["candidate"]
    interviewer = state["interviewer"]
    cid = candidate.id

    proposed_dt = state.get("proposed_interview_time")
    if proposed_dt is None:
        # 正常到不了这里（confirm 也会拦），真出现说明上游状态错乱
        raise InterviewSchedulingError(
            f"候选人 {cid} 缺少最终面试时间，无法复核（proposed_interview_time 为空）"
        )

    logger.info(
        "[recheck] start | candidate_id=%s | interviewer_id=%s | time=%s",
        cid, interviewer.id, proposed_dt,
    )

    # 时段已经过去就不用再查钉钉了——面试官不可能回到过去。
    # 协商拖得久（跨周末、跨假期）时，候选人确认的可能是个已经过去的时间。
    now = now_beijing()
    if proposed_dt <= now:
        logger.info(
            "[recheck] 时段已过去，按不可用处理 | candidate_id=%s | time=%s | now=%s",
            cid, proposed_dt, now,
        )
        await _notify_slot_taken(candidate, proposed_dt)
        return {
            "candidate_email_reply": REPLY_SLOT_TAKEN,
            "negotiation_round": 0,
            "current_stage": "negotiation",
        }

    try:
        available_slots = await get_user_available_slots(
            interviewer_id=interviewer.id,
            hour_interval=INTERVIEW_DURATION.total_seconds() / 3600,
            max_days_to_search=1,
            start_time=proposed_dt.replace(hour=0, minute=0, second=0, microsecond=0),
            time_delta=0,
        )
    except DingTalkServiceError as e:
        logger.error("[recheck] get_available_slots failed | candidate_id=%s | error=%s", cid, e)
        raise

    still_free = resolve_interview_slot(
        _STRATEGY, available_slots, proposed_dt, INTERVIEW_DURATION
    )

    if still_free is not None:
        logger.info("[recheck] slot still free | candidate_id=%s", cid)
        return {
            "candidate_email_reply": REPLY_TIME_AVAILABLE,
            "current_stage": "confirm",
        }

    logger.info(
        "[recheck] slot taken during negotiation | candidate_id=%s | time=%s",
        cid, proposed_dt,
    )
    await _notify_slot_taken(candidate, proposed_dt)

    return {
        # 路由靠这个标记区分「去 confirm」还是「回邮件节点」
        "candidate_email_reply": REPLY_SLOT_TAKEN,
        # 归零：给候选人重新协商的完整轮次，而不是接着上一次计数走
        "negotiation_round": 0,
        "current_stage": "negotiation",
    }
