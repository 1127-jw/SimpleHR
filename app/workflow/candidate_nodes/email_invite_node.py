from datetime import datetime

from loguru import logger

from app.service.dingtalk_ser import get_user_available_slots
from app.workflow.candidate_state import CandidateRecruitmentState
from utils.emailbot.mail_manager import get_mail_bot_
from utils.time_ISO8601_datatime import now_beijing


async def send_interview_email(email: str, body: str) -> None:
    """发送面试邀请邮件"""
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
    except Exception as e:
        logger.error(f"发送邮件失败：{e}")
        raise ValueError(f"给候选人发送邮件失败：{e}")


async def email_invite_node(state: CandidateRecruitmentState) -> dict:
    """
    发送面试邀请邮件节点。
    复用 agents/candidate.py L135-L187 的 get_interviewer_available_slot 工具逻辑
    和 L189-L229 的 send_interview_email 工具逻辑。
    """
    interviewer = state["interviewer"]
    candidate = state["candidate"]
    position = state["position"]
    available_slots = await get_user_available_slots(interviewer.id)

    if not available_slots:
        try:
            body = f"""
尊敬的{candidate.name}，
您好！
感谢您投递我司{position.title}职位。
未来7天内面试官暂时没有时间面试您，可以将面试时间改到7天后吗？
谢谢！
"""
            await send_interview_email(candidate.email, body=body)
        except Exception as e:
            logger.error(e)
            raise ValueError(f"给候选人发送邮件失败：{e}")
        return {
            "email_sent": True,
            "email_sent_at": None,
            "current_stage": "negotiation",  # 无空闲时间也进入协商
            "candidate_email_reply": "SYSTEM_NO_SLOT",
            "negotiation_round": 0,
        }

    # 取第一个可用时间段作为建议面试时间
    proposed_time = available_slots[0][0]

    # Step 2：发送邮件
    body = f"""
    尊敬的{candidate.name}，
    您好！
    感谢您投递我司{position.title}职位。
    我们初步确定了您的面试时间，请您确认是否方便。
    面试时间：{proposed_time}
    请您确认是否方便，如果方便，请您回复“确认”。
    如果不方便，请回复您方便的时间，我们将会重新协商面试时间。
    谢谢！
                """
    try:
        await send_interview_email(candidate.email, body=body)
    except Exception as e:
        logger.error(e)
        raise ValueError(f"给候选人发送邮件失败：{e}")

    return {
        "email_sent": True,
        "email_sent_at": now_beijing(),
        "current_stage": "negotiation",
        "proposed_interview_time": proposed_time,
        "negotiation_round": 0,
    }
