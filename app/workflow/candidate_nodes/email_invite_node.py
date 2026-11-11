import logging
from datetime import datetime

from app.core.config import get_settings
from app.core.db import AsyncSessionFactory
from app.core.exceptions import DingTalkServiceError, EmailSendError
from app.repository.resume_repo import CandidateRepo
from app.service.dingtalk_ser import get_user_available_slots
from app.workflow.candidate_state import CandidateRecruitmentState
from app.workflow.error_handler import node_error_wrapper
from utils.time_ISO8601_datatime import now_beijing, to_naive_beijing

settings = get_settings()
logger = logging.getLogger(settings.NODE_LOGGER_NAME)


async def _mark_invite_sent(candidate_id: str) -> None:
    """记录邀请邮件发送时间。

    该时间戳是「等待候选人回复」超时巡检的判据：只有确实停在
    ``get_candidate_email_reply`` 中断点、且超过 ``EMAIL_REPLY_TIMEOUT_HOURS``
    的候选人才会被推进到「协商超时」。
    """
    async with AsyncSessionFactory() as session:
        async with session.begin():
            await CandidateRepo(session).update_email_invite_sent_at(
                candidate_id=candidate_id,
                sent_at=to_naive_beijing(now_beijing()),
            )


async def send_interview_email(email: str, body: str) -> None:
    """发送面试邀请邮件。

    Raises:
        EmailSendError: 邮件发送失败。
    """
    from utils.emailbot.mail_manager import get_mail_bot_
    email_bot = get_mail_bot_()
    subject = "面试邀请-协商面试时间"

    try:
        if not email_bot.is_connected:
            await email_bot.connect()
        await email_bot.send_email(
            to=email,
            subject=subject,
            text=body,
        )
    except EmailSendError:
        raise
    except Exception as e:
        logger.error("[email] send failed | to=%s | error=%s", email, e)
        raise EmailSendError(f"发送面试邀请邮件失败: {e}") from e


@node_error_wrapper
async def email_invite_node(state: CandidateRecruitmentState) -> dict:
    """
    发送面试邀请邮件节点。
    复用 agents/candidate.py L135-L187 的 get_interviewer_available_slot 工具逻辑
    和 L189-L229 的 send_interview_email 工具逻辑。
    """
    interviewer = state["interviewer"]
    candidate = state["candidate"]
    position = state["position"]
    cid = candidate.id

    logger.info("[email_invite] start | candidate_id=%s", cid)

    try:
        available_slots = await get_user_available_slots(interviewer.id)
    except DingTalkServiceError:
        raise
    except Exception as e:
        logger.error("[email_invite] get_available_slots failed | candidate_id=%s | error=%s", cid, e)
        raise DingTalkServiceError(
            f"获取面试官可用时间失败: {e}"
        ) from e

    if not available_slots:
        logger.info(
            "[email_invite] no available slots within 7 days | candidate_id=%s",
            cid,
        )
        body = f"""
尊敬的{candidate.name}，
您好！
感谢您投递我司{position.title}职位。
未来7天内面试官暂时没有时间面试您，可以将面试时间改到7天后吗？
谢谢！
"""
        await send_interview_email(candidate.email, body=body)
        logger.info("[email_invite] no-slot notice sent | candidate_id=%s", cid)
        # 无空闲时间同样进入协商等待，记录发送时间供超时巡检使用
        try:
            await _mark_invite_sent(cid)
        except Exception as e:
            logger.error(
                "[email_invite] mark invite sent failed | candidate_id=%s | error=%s", cid, e
            )
        return {
            "email_sent": True,
            "email_sent_at": None,
            "current_stage": "negotiation",  # 无空闲时间也进入协商
            "candidate_email_reply": "SYSTEM_NO_SLOT",
            "negotiation_round": 0,
        }

    proposed_time = available_slots[0][0]
    logger.info(
        "[email_invite] proposed time | candidate_id=%s | time=%s",
        cid, proposed_time,
    )

    body = f"""
    尊敬的{candidate.name}，
    您好！
    感谢您投递我司{position.title}职位。
    我们初步确定了您的面试时间，请您确认是否方便。
    面试时间：{proposed_time}
    请您确认是否方便，如果方便，请您回复"确认"。
    如果不方便，请回复您方便的时间，我们将会重新协商面试时间。
    谢谢！
                """
    await send_interview_email(candidate.email, body=body)

    logger.info("[email_invite] invite sent | candidate_id=%s", cid)

    # 记录发送时间：这是「等待候选人回复」超时巡检的起点
    try:
        await _mark_invite_sent(cid)
    except Exception as e:
        logger.error(
            "[email_invite] mark invite sent failed | candidate_id=%s | error=%s", cid, e
        )

    return {
        "email_sent": True,
        "email_sent_at": now_beijing(),
        "current_stage": "negotiation",
        "proposed_interview_time": proposed_time,
        "negotiation_round": 0,
    }
