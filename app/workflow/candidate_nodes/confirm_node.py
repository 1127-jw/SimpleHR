import logging
from datetime import datetime, timedelta

from app.agents.candidate import get_access_token
from app.core.config import get_settings
from app.core.db import AsyncSessionFactory
from app.core.exceptions import (
    DingTalkServiceError,
    EmailSendError,
    InterviewSchedulingError,
)
from app.models.InterviewModel import InterviewResultEnum
from app.models.candidate import CandidateStatusEnum
from app.repository.Interview_repo import InterviewRepo
from app.repository.resume_repo import CandidateRepo
from app.service.dingtalk_ser import create_calendar
from app.workflow.candidate_state import CandidateRecruitmentState
from app.workflow.error_handler import node_error_wrapper
from app.core.time import to_naive_beijing

settings = get_settings()
logger = logging.getLogger(settings.NODE_LOGGER_NAME)


@node_error_wrapper
async def confirm_node(state: CandidateRecruitmentState) -> dict:
    """
    确认最终面试时间。
    包含：发确认邮件 + 创建钉钉日程 + 创建面试记录 + 更新候选人状态。
    """
    position = state['position']
    candidate = state["candidate"]
    interviewer = state["interviewer"]
    cid = candidate.id

    logger.info("[confirm] start | candidate_id=%s", cid)

    # 缺这个字段只可能来自上游状态错乱（例如「无空闲时段」分支没有确定时间），
    # 直接抛 KeyError 会变成 AppException 且看不出原因，这里给出可读的业务异常。
    interview_datetime: datetime = state.get("proposed_interview_time")
    if interview_datetime is None:
        raise InterviewSchedulingError(
            f"候选人 {cid} 缺少最终面试时间，无法确认（proposed_interview_time 为空）"
        )

    # 统一使用 to_naive_beijing 剥离时区（确保存入 DB 的是北京时间数值）
    interview_datetime_without_tz = to_naive_beijing(interview_datetime)

    # 1. 获取面试官钉钉 token
    try:
        result = await get_access_token(interviewer)
        if isinstance(result, str):
            raise DingTalkServiceError(result)
        union_id, access_token_info = result
        access_token = access_token_info.get("access_token")
    except DingTalkServiceError:
        raise
    except Exception as e:
        logger.error("[confirm] get DingTalk token failed | candidate_id=%s | error=%s", cid, e)
        raise DingTalkServiceError(
            f"获取面试官钉钉授权失败: {e}"
        ) from e

    # 2. 创建钉钉日程（外部系统，先做）
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
        logger.error("[confirm] create calendar failed | candidate_id=%s | error=%s", cid, e)
        raise DingTalkServiceError(
            f"创建钉钉面试日程失败: {e}"
        ) from e

    logger.info("[confirm] calendar created | candidate_id=%s", cid)

    # 3. 创建或更新面试预约记录 + 更新候选人状态
    try:
        async with AsyncSessionFactory() as session:
            async with session.begin():
                interview_repo = InterviewRepo(session)
                candidate_repo = CandidateRepo(session)

                # 检查是否已存在面试记录（candidate_id 有唯一约束）
                existing_interview = await interview_repo.get_by_candidate_id(cid)
                if existing_interview:
                    # 已存在 → 更新面试时间（人工重试场景）
                    await interview_repo.update_interview(
                        interview_id=existing_interview.id,
                        interview_dict={
                            "scheduled_time": interview_datetime_without_tz,
                            "result": InterviewResultEnum.PENDING,
                            "interviewer_id": interviewer.id,
                        },
                    )
                    logger.info("[confirm] interview record updated | candidate_id=%s", cid)
                else:
                    # 不存在 → 创建新记录
                    await interview_repo.create_interview({
                        "scheduled_time": interview_datetime_without_tz,
                        "result": InterviewResultEnum.PENDING,
                        "candidate_id": cid,
                        "interviewer_id": interviewer.id,
                    })
                    logger.info("[confirm] interview record created | candidate_id=%s", cid)

                await candidate_repo.update_candidate_status(
                    candidate_id=cid,
                    status=CandidateStatusEnum.WAITING_FOR_INTERVIEW,
                )
    except Exception as e:
        logger.error("[confirm] DB write failed | candidate_id=%s | error=%s", cid, e)
        raise InterviewSchedulingError(
            f"创建面试预约记录失败: {e}"
        ) from e

    logger.info("[confirm] interview record created | candidate_id=%s", cid)

    # 4. 发送确认邮件
    subject = "面试时间确定"
    body = f"""
    尊敬的{candidate.name}，
    面试时间已确定：
    {interview_datetime}
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
    except EmailSendError:
        raise
    except Exception as e:
        logger.error("[confirm] send confirmation email failed | candidate_id=%s | error=%s", cid, e)
        raise EmailSendError(
            f"发送面试确认邮件失败: {e}"
        ) from e

    logger.info("[confirm] done | candidate_id=%s", cid)

    return {
        "confirmed_interview_time": interview_datetime,
        "interview_created": True,
        "current_stage": "confirmed",
    }
