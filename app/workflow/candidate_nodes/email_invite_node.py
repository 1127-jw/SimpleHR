from datetime import datetime,timedelta

from app.workflow.candidate_state import CandidateRecruitmentState
from app.core.db import AsyncSessionFactory
from app.core.redis_client import get_redis
from app.models import DingdingUserModel
from app.repository.user_repo import UserRepo
from app.schemas.user_schema import UserSH
from app.service.dingtalk_ser import get_calendar_list, get_union_id_and_access_token_by_user_id, \
    get_user_available_slots
from loguru import logger
from typing import cast, Any

from utils.available_time import find_available_slot
from utils.emailbot.mail_manager import get_mail_bot_
from utils.time_ISO8601_datatime import iso8601_to_datetime_beijing, datetime_to_iso8601_beijing


async def email_invite_node(state: CandidateRecruitmentState) -> dict:
    """
    发送面试邀请邮件节点。
    复用 agents/candidate.py L135-L187 的 get_interviewer_available_slot 工具逻辑
    和 L189-L229 的 send_interview_email 工具逻辑。
    """
    interviewer = state.interviewer
    candidate = state.candidate
    position = state.position

    available_slots = await get_user_available_slots(interviewer.id)

    if not available_slots:
        return {
            "email_sent": False,
            "email_sent_at": None,
            "current_stage": "negotiation",  # 无空闲时间也进入协商
            "candidate_email_reply": "SYSTEM_NO_SLOT"
        }

    # 取第一个可用时间段作为建议面试时间
    proposed_time = available_slots[0][0]

    # Step 2：发送邮件
    email_bot = get_mail_bot_()

    subject = "【知了课堂】面试邀请-协商面试时间"
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
        await email_bot.send_email(
            to=candidate.email,
            subject=subject,
            text=body,
        )
    except Exception as e:
        logger.error(e)
        raise f"给候选人发送邮件失败：{e}"

    return {
        "email_sent": True,
        "email_sent_at": datetime_to_iso8601_beijing(datetime.now()),
        "current_stage": "negotiation",
        "proposed_interview_time": datetime_to_iso8601_beijing(proposed_time)
    }