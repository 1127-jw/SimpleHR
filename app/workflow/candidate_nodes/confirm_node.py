from datetime import datetime, timedelta
from app.agents.candidate import get_access_token
from app.service.dingtalk_ser import create_calendar
from app.core.db import AsyncSessionFactory
from app.models.InterviewModel import InterviewResultEnum
from app.models.candidate import CandidateStatusEnum
from app.repository.Interview_repo import InterviewRepo
from app.repository.resume_repo import CandidateRepo
from utils.emailbot.mail_manager import get_mail_bot_
from app.workflow.candidate_state import CandidateRecruitmentState
from utils.time_ISO8601_datatime import to_naive_beijing, to_iso8601
from loguru import logger


async def confirm_node(state: CandidateRecruitmentState) -> dict:
    """
    确认最终面试时间。
    包含：发确认邮件 + 创建钉钉日程 + 创建面试记录 + 更新候选人状态。
    """
    position = state['position']
    candidate = state['candidate']
    interviewer = state['interviewer']

    interview_datetime: datetime = state["proposed_interview_time"]
    # 统一使用 to_naive_beijing 剥离时区（确保存入 DB 的是北京时间数值）
    interview_datetime_without_tz = to_naive_beijing(interview_datetime)

    try:
        async with AsyncSessionFactory() as session:
            async with session.begin():
                interview_repo = InterviewRepo(session)
                # 3. 在数据库中创建一个面试预约记录
                await interview_repo.create_interview({
                    # 面试时间因为是存储到postgresql数据库中，现在是只能存储没有带时区的日期
                    "scheduled_time": interview_datetime_without_tz,
                    "result": InterviewResultEnum.PENDING,
                    "candidate_id": candidate.id,
                    "interviewer_id": interviewer.id,
                })
                # 4. 在数据库中修改候选人的状态为待面试
                candidate_repo = CandidateRepo(session)
                await candidate_repo.update_candidate_status(
                    candidate_id=candidate.id,
                    status=CandidateStatusEnum.WAITING_FOR_INTERVIEW,
                )
    except Exception as e:
        raise ValueError(f"在系统中创建面试预约记录和候选人状态修改失败！{e}")

    result = await get_access_token(interviewer)
    # 错误时返回字符串，成功时返回元组
    if isinstance(result, str):
        # 节点必须返回 dict（状态更新），不能返回字符串；此处改为抛异常
        raise ValueError(result)
    union_id, access_token_info = result
    access_token = access_token_info.get("access_token")

    try:
        end_datetime: datetime = interview_datetime + timedelta(hours=1)
        await create_calendar(
            union_id=union_id,
            access_token=access_token,
            summary=f"面试安排：{position.title} - {candidate.name}",
            start_datetime=interview_datetime,
            end_datetime=end_datetime,
        )
    except Exception as e:
        raise ValueError(f"创建钉钉面试日程安排失败，错误信息：\n{e}")



    subject = "面试时间确定"
    body = f"""
    "尊敬的{candidate.name}，
    面试时间已确定：
    {interview_datetime}
    请您准时参加面试。该邮件无需再回复。谢谢！
    """
    email_bot = get_mail_bot_()
    try:
        await email_bot.send_email(
            to=candidate.email,
            subject=subject,
            text=body,
        )
    except Exception as e:
        logger.error(e)
        raise ValueError(f"给候选人发送邮件失败：{e}")


    return {
            "confirmed_interview_time": state["proposed_interview_time"],
            "interview_created": True,
            "current_stage": "confirmed"
        }