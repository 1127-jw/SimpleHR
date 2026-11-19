import logging
from datetime import datetime

from app.core.config import get_settings
from app.core.db import AsyncSessionFactory
from app.core.exceptions import DingTalkServiceError
from app.models.candidate import CandidateStatusEnum
from app.repository.candidate_history_repo import CandidateStatusHistoryRepo
from app.repository.resume_repo import CandidateRepo
from app.service.dingtalk_ser import get_user_available_slots
from app.workflow.candidate_nodes.mail_notify import send_workflow_email
from app.workflow.candidate_state import CandidateRecruitmentState
from app.workflow.error_handler import node_error_wrapper
from app.core.time import db_now, now_beijing

settings = get_settings()
logger = logging.getLogger(settings.NODE_LOGGER_NAME)

INVITE_SUBJECT = "面试邀请-协商面试时间"


def _fmt_slot(dt) -> str:
    """时间段格式化：``available_slots`` 里是 aware datetime，直接按墙钟输出。"""
    return dt.strftime("%Y-%m-%d %H:%M")


async def _mark_invite_sent(candidate_id: str) -> None:
    """记录邀请邮件发送时间。

    该时间戳是「等待候选人回复」超时巡检的**兜底起点**；主判据已改为
    状态变更链的 ``COALESCE(last_email_received_at, changed_at)``。
    """
    async with AsyncSessionFactory() as session:
        async with session.begin():
            await CandidateRepo(session).update_email_invite_sent_at(
                candidate_id=candidate_id,
                sent_at=db_now(),
            )


async def _enter_negotiation(candidate_id, candidate, position) -> None:
    """进入「面试时间协商中」：写 candidates.status + 状态变更链。

    链表里这一条是超时巡检的取数来源，**必须写**；漏掉会让这条流程
    永远扫不到（既不超时也不推进）。
    """
    async with AsyncSessionFactory() as session:
        async with session.begin():
            await CandidateRepo(session).update_candidate_status(
                candidate_id=candidate_id,
                status=CandidateStatusEnum.NEGOTIATING,
            )
            await CandidateStatusHistoryRepo(session).record_change(
                candidate_id,
                to_status=CandidateStatusEnum.NEGOTIATING.value,
                from_status=str(getattr(candidate, "status", "") or None),
                candidate_email=getattr(candidate, "email", None),
                position_id=getattr(position, "id", None),
                position_title=getattr(position, "title", None),
            )


@node_error_wrapper
async def email_invite_node(state: CandidateRecruitmentState) -> dict:
    """
    发送面试邀请邮件节点。
    复用 agents/candidate.py L135-L187 的 get_interviewer_available_slot 工具逻辑
    和 L189-L229 的邀请邮件发送逻辑。
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
        await send_workflow_email(candidate.email, subject=INVITE_SUBJECT, text=body)
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

    # 正文里列出最近 3 段可用时间：只给一个时间时，候选人一旦不方便
    # 就得再往返回一封邮件才能拿到备选，来一回就过去一天。
    slot_list = "\n".join(
        f"{i}. {_fmt_slot(start)} - {_fmt_slot(end)}"
        for i, (start, end) in enumerate(available_slots[:3], start=1)
    )

    body = f"""
尊敬的{candidate.name}，
您好！
感谢您投递我司{position.title}职位。
面试官近期有以下时间段可供面试（按时间先后）：
{slot_list}
请您回复方便的时间（例如「确认」表示第一个时间段）。
如果以上都不方便，请回复您方便的时间，我们将会重新协商面试时间。
谢谢！
                """
    await send_workflow_email(candidate.email, subject=INVITE_SUBJECT, text=body)

    logger.info("[email_invite] invite sent | candidate_id=%s", cid)

    # 记录发送时间：这是「等待候选人回复」超时巡检的起点
    try:
        await _mark_invite_sent(cid)
    except Exception as e:
        logger.error(
            "[email_invite] mark invite sent failed | candidate_id=%s | error=%s", cid, e
        )

    # 进入「面试时间协商中」：既写 candidates.status，也写状态变更链。
    # 链表里这条是超时巡检的判据来源（状态=协商中 + 链上最新 + 起算超时）。
    try:
        await _enter_negotiation(cid, candidate, position)
    except Exception as e:
        # 写链失败不影响发信结果，但必须让人看到——否则巡检会漏掉这条流程
        logger.error(
            "[email_invite] 写入协商状态/链记录失败 | candidate_id=%s | error=%s", cid, e
        )

    return {
        "email_sent": True,
        "email_sent_at": now_beijing(),
        "current_stage": "negotiation",
        "proposed_interview_time": proposed_time,
        "negotiation_round": 0,
    }
