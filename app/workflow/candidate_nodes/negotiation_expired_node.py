import logging

from app.core.config import get_settings
from app.core.db import AsyncSessionFactory
from app.models.candidate import CandidateStatusEnum
from app.repository.resume_repo import CandidateRepo
from app.workflow.candidate_state import CandidateRecruitmentState
from app.workflow.error_handler import node_error_wrapper

settings = get_settings()
logger = logging.getLogger(settings.NODE_LOGGER_NAME)


async def _notify_candidate_expired(candidate) -> None:
    """best-effort 通知：协商超时。

    此前这个节点只改库、不发信，候选人从「邀请邮件」之后就再没收到任何消息，
    直到 HR 主动联系。发信失败只记日志，不影响状态推进。
    """
    try:
        from utils.emailbot.mail_manager import get_mail_bot_

        email_bot = get_mail_bot_()
        if not email_bot.is_connected:
            await email_bot.connect()
        body = f"""
尊敬的{candidate.name}，
您好！
我们多次尝试与您确认面试时间，但暂未收到有效回复，本次面试时间协商已暂停。
如您仍对该职位感兴趣，欢迎重新投递简历，我们会尽快重新安排。
谢谢！
"""
        await email_bot.send_email(
            to=candidate.email,
            subject="面试时间协商-已暂停",
            text=body,
        )
        logger.info(
            "[negotiation_expired] 已回信告知协商暂停 | candidate_id=%s",
            getattr(candidate, "id", None),
        )
    except Exception:  # noqa: BLE001 - 通知属于增强项，不能拖垮状态流转
        logger.warning(
            "[negotiation_expired] 回信失败（不影响状态推进） | candidate_id=%s",
            getattr(candidate, "id", None), exc_info=True,
        )


@node_error_wrapper
async def negotiation_expired_node(state: CandidateRecruitmentState) -> dict:
    """协商超时/超过协商轮次上限 → 更新候选人状态为协商超时。"""
    cid = state["candidate"].id
    logger.info(
        "[negotiation_expired] start | candidate_id=%s | round=%d",
        cid, state.get("negotiation_round", 0),
    )

    async with AsyncSessionFactory() as session:
        async with session.begin():
            repo = CandidateRepo(session)
            await repo.update_candidate_status(
                candidate_id=cid, status=CandidateStatusEnum.NEGOTIATION_EXPIRED,
            )

    logger.info("[negotiation_expired] status updated | candidate_id=%s", cid)

    await _notify_candidate_expired(state["candidate"])

    return {"current_stage": "negotiation_expired"}
