"""邮件命中后的业务分发：意图管道（L0→L1→L2→L3）+ 动作分发（L4）。

与旧实现的差别：不再用 ``rule.name`` 当意图真值，而是先识别意图、
再校验流程状态与 checkpoint，最后才决定是否恢复图 / 落待确认 / 通知人工。
"""
from __future__ import annotations

import asyncio
import logging

from langgraph.types import Command

from utils.emailbot.intent import (
    MailIntent,
    MailIntentResult,
    SenderRole,
    classify_intent,
)
from utils.emailbot.models import ParsedEmail
from utils.emailbot.rules import MailRule

from app.core.db import AsyncSessionFactory
from app.core.checkpointer import get_checkpointer
from app.core.config import get_settings
from app.core.time import db_now
from app.models.candidate import CandidateStatusEnum
from app.repository.candidate_history_repo import CandidateStatusHistoryRepo
from app.repository.resume_repo import CandidateRepo
from app.schemas.candidate_schema import CandidateSchema
from app.schemas.position_schema import PositionSchema
from app.schemas.user_schema import UserSH
from app.workflow.candidate_graph import build_candidate_graph
from app.workflow.candidate_state import EMAIL_REPLY_INTERRUPT, is_waiting_on, thread_id_of

logger = logging.getLogger(__name__)

settings = get_settings()

# 允许恢复协商图的候选人状态（与既有兜底逻辑保持一致）。
# NEGOTIATING 是「进入面试时间协商」后的新状态，漏掉它会导致回复邮件不恢复图。
_RESUMABLE_STATUS = (
    CandidateStatusEnum.MANUAL_PASSED,
    CandidateStatusEnum.AI_FILTER_PASSED,
    CandidateStatusEnum.NEGOTIATING,
)


async def _find_candidate_by_email(email_address: str):
    """根据邮箱地址查找候选人（含职位与面试官）。

    **同一邮箱可能有多条候选人记录**（多次投递）。``get_by_email`` 返回的是
    任意一条，会把流程恢复到错误的记录上；这里按状态变更链挑「当前活跃」的那条
    （``next_id IS NULL``），没有活跃链再退回最新创建的。
    """
    async with AsyncSessionFactory() as session:
        rows = await CandidateRepo(session).list_by_email(email_address)
        if not rows:
            return None, None, None

        history = CandidateStatusHistoryRepo(session)
        chosen = None
        for row in rows:
            if await history.get_active(row.id) is not None:
                chosen = row
                break
        if chosen is None:
            chosen = rows[0]

        # 必须在 session 内访问关联对象并转 Schema，避免 DetachedInstanceError
        return (
            CandidateSchema.model_validate(chosen),
            PositionSchema.model_validate(chosen.position),
            UserSH.model_validate(chosen.creator),
        )


# 模块级缓存：复用同一个编译图实例读取状态快照，避免每次邮件轮询重建
_graph = None


def _get_graph():
    global _graph
    if _graph is None:
        _graph = build_candidate_graph(get_checkpointer())
    return _graph


async def _waiting_reply(thread_id: str) -> bool:
    """该 thread 的图是否停在「等待候选人回复」这个中断点。

    不能用 ``bool(snapshot.next)``：人工闸门（manual_decision）也是中断点，
    ``next`` 同样非空。那样停在闸门的候选人来信时会被喂成
    ``waiting_reply=True``，意图分类器更容易把泛泛的「好的」误判成
    ``INTERVIEW_REPLY``。这里与恢复侧使用同一套中断点身份判定。
    """
    try:
        graph = _get_graph()
        snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    except Exception:
        logger.exception("读取 checkpoint 失败 | thread_id=%s", thread_id)
        return False
    if not snapshot:
        return False
    return is_waiting_on(snapshot.interrupts, EMAIL_REPLY_INTERRUPT)


async def notify(email: ParsedEmail, rule: MailRule | None = None) -> MailIntentResult | None:
    """邮件命中后的回调：识别意图 → 校验 → 分发动作。

    Args:
        email: 已解析的邮件。
        rule: 命中的规则（仅作先验特征，不再作为意图真值）。

    Returns:
        分类结果，供调用方（MailBot）决定自动回复。
    """
    # ---- L1 身份识别 ----
    candidate_schema, position_schema, interviewer_schema = \
        await _find_candidate_by_email(email.from_.address)

    if candidate_schema:
        role = SenderRole.CANDIDATE
        candidate_status = str(candidate_schema.status)
        # thread_id 与建图时保持一致：按 candidate_id 隔离（见 app/tasks/agent_tasks.py）
        thread_id = thread_id_of(candidate_schema.id)
    else:
        role = SenderRole.UNKNOWN
        candidate_status = ""
        thread_id = f"unknown:{email.from_.address}"

    # ---- L3 前置校验：是否在等待回复 ----
    waiting_reply = await _waiting_reply(thread_id) if candidate_schema else False

    # 收到候选人本人的邮件 → 更新「最后接到邮件时间」，供超时巡检做判据。
    # 只有 CANDIDATE 才算：面试官邮件 / 系统通知会把 48h 超时重置掉。
    if candidate_schema and role == SenderRole.CANDIDATE:
        try:
            async with AsyncSessionFactory() as session:
                async with session.begin():
                    touched = await CandidateStatusHistoryRepo(session).touch_last_email(
                        candidate_schema.id, received_at=db_now()
                    )
            if not touched:
                logger.info(
                    "[notify] 无活跃链记录，跳过更新时间 | candidate_id=%s",
                    candidate_schema.id,
                )
        except Exception:
            logger.exception(
                "[notify] 更新最后邮件时间失败 | candidate_id=%s", candidate_schema.id
            )

    # ---- L2 意图分类 ----
    result = await classify_intent(
        email,
        role=role,
        candidate_status=candidate_status,
        waiting_reply=waiting_reply,
        has_attachment=bool(email.attachments),
        prior_intents=getattr(email, "prior_intents", None),
    )
    logger.info(
        "[intent] uid=%s | intent=%s | sub=%s | conf=%.2f | review=%s | reason=%s",
        email.uid, result.intent.value, result.sub_intent,
        result.confidence, result.needs_review, result.reason,
    )

    # ---- L4 动作分发 ----
    # 自动回复/OOO/退信/系统通知
    if result.intent is MailIntent.AUTO_REPLY:
        return result

    # 新简历投递：无论置信度高低都先落 PENDING，由 HR 在投递确认台人工确认。
    if result.intent is MailIntent.RESUME_SUBMISSION:
        await _handle_resume_submission(email)
        return result
    # 低置信，转人工
    if result.needs_review or result.intent is MailIntent.AMBIGUOUS:
        await _enqueue_human_review(email, result)
        return result
    # 面试协商回复
    if result.intent is MailIntent.INTERVIEW_REPLY:
        resumed = await _resume_candidate_graph(
            email, candidate_schema, position_schema, interviewer_schema
        )
        if not resumed:
            # 候选人的回复没有对应的等待中断点（流程停在别处 / 邀请还没发）。
            # 这封邮件对 HR 有价值，落进「意图复核」，别让它石沉大海。
            await _enqueue_human_review(email, result)
        return result
    # 询问进度/结果
    if result.intent is MailIntent.PROGRESS_INQUIRY:
        await _reply_progress(email, candidate_schema)
        return result
    # 面试官邮件
    if result.intent is MailIntent.INTERVIEWER_MAIL:
        await _notify_hr(email, result)
        return result

    # 无关邮件 OUT_OF_SCOPE：忽略
    return result


async def _resume_candidate_graph(
    email: ParsedEmail,
    candidate_schema: CandidateSchema | None,
    position_schema: PositionSchema | None,
    interviewer_schema: UserSH | None,
) -> bool:
    """恢复招聘图：仅在候选人在协商中、且 checkpoint 停在等待回复中断点。

    Returns:
        True 表示已发起恢复；False 表示状态不匹配、**没有**恢复。
        调用方据此把这封邮件落进「意图复核」，避免候选人的回复石沉大海。
    """
    if not candidate_schema:
        logger.warning("未找到候选人，不恢复图 | from=%s", email.from_.address)
        return False

    if candidate_schema.status not in _RESUMABLE_STATUS:
        logger.info(
            "候选人不在协商中，不恢复图 | candidate_id=%s | status=%s",
            candidate_schema.id, candidate_schema.status,
        )
        return False

    thread_id = thread_id_of(candidate_schema.id)
    # 复用模块级编译图，避免每次恢复都重新编译
    candidate_graph = _get_graph()

    # 身份校验：只 resume「等待候选人回复」这个中断点。
    # 只看 ``next`` 非空不够——若 thread 已被消费过（重复处理同一封邮件）或状态异常，
    # ``resume`` 会让图**从头重跑**，state 里的 skip_scoring 残留会再发一次邀请邮件
    # （实测：候选人收到「邀请」+「时段不可用」两封）。
    snapshot = await candidate_graph.aget_state({"configurable": {"thread_id": thread_id}})
    if not is_waiting_on(snapshot.interrupts, EMAIL_REPLY_INTERRUPT):
        logger.info(
            "[notify] 中断点不是等待候选人回复，不恢复图 | candidate_id=%s | thread_id=%s",
            candidate_schema.id, thread_id,
        )
        return False

    if not settings.GRAPH_OPEN:
        logger.info("GRAPH_OPEN=false，仅记录不执行 | thread_id=%s", thread_id)
        return False

    from app.core.limiters import task_slot
    from app.service.candidate_status_ser import mark_candidate_error

    # 与启动流程共用 "recruit" 名额：限制同时进行的招聘流程总数
    try:
        async with task_slot("recruit", settings.RECRUIT_TASK_MAX_CONCURRENCY):
            # 恢复侧是唯一需要 thread_id 的地方：挂起在哪个节点、又被哪次 resume
            # 唤醒，只有这条日志能把两者对上（失败路径原本就带 thread_id）
            logger.info(
                "[notify] 恢复招聘图 | candidate_id=%s | thread_id=%s",
                candidate_schema.id, thread_id,
            )
            await candidate_graph.ainvoke(
                Command(
                    resume=(
                        "候选人回复了以下邮件，请根据回复内容决定下一步操作：\n\n"
                        f"主题：{email.subject}\n\n内容：{email.text}"
                    ),
                    update={
                        # 刷新候选人/职位/面试官信息（防止检查点中的旧数据）
                        "candidate": candidate_schema,
                        "position": position_schema,
                        "interviewer": interviewer_schema,
                    },
                ),
                config={"configurable": {"thread_id": thread_id}},
            )
    except Exception as e:
        # 后台链路：异常不会经过 FastAPI 全局处理器，必须就地处理
        logger.error(
            "[notify] 恢复图失败 | candidate_id=%s | thread_id=%s | error=%s",
            candidate_schema.id, thread_id, e, exc_info=True,
        )
        await mark_candidate_error(candidate_schema.id, f"恢复招聘图失败: {e}")
        return False

    return True


async def _handle_resume_submission(email: ParsedEmail) -> None:
    """新简历投递：落「待确认」记录 + 通知 HR，不自动建人、不起图。"""
    from app.service.mail_submission_ser import (
        create_mail_resume_submission,
        enrich_mail_submission,
        notify_hr_new_submission,
    )

    sub = await create_mail_resume_submission(email)
    await notify_hr_new_submission(email)

    # best-effort：后台解析简历并回填 parsed_info，避免拖慢邮件轮询
    paths = [str(a.saved_path) for a in email.attachments if a.saved_path]
    if paths:
        asyncio.create_task(enrich_mail_submission(sub.id, paths[0]))


async def _reply_progress(email: ParsedEmail, candidate_schema) -> None:
    """进度询问：记录日志（自动回复文案由 bot 侧按意图决定）。
    """
    logger.info(
        "[progress_inquiry] 来自 %s | candidate_id=%s",
        email.from_.address, candidate_schema.id if candidate_schema else None,
    )


async def _notify_hr(email: ParsedEmail, result: MailIntentResult) -> None:
    """面试官邮件：记录日志，交由人工处理。

    TODO: 接入实际通知（钉钉工作通知 / 邮件），可复用
    ``app/service/mail_submission_ser.py`` 里的邮件通知实现；
    注意当前 L1 尚未识别面试官身份，本分支主要靠 LLM 分类命中。
    """
    logger.info(
        "[notify_hr] 来自 %s | intent=%s | evidence=%s",
        email.from_.address, result.intent.value, result.evidence,
    )


async def _enqueue_human_review(email: ParsedEmail, result: MailIntentResult) -> None:
    """低置信 / 歧义：不 resume、不自动回，**落库**等人工复核。

    复核人在「候选人管理 → 意图复核」里确认意图并执行动作
    （见 ``app/service/mail_review_ser.py``）。

    落库失败只记日志、不影响邮件轮询：这封邮件最坏就是没人复核，
    不该让整轮 poll 崩掉。
    """
    logger.warning(
        "[human_review] uid=%s | intent=%s | conf=%.2f | reason=%s",
        email.uid, result.intent.value, result.confidence, result.reason,
    )
    try:
        from app.service.mail_review_ser import enqueue_review

        async with AsyncSessionFactory() as session:
            async with session.begin():
                _, created = await enqueue_review(session, email, result)
        if created:
            logger.info(
                "[human_review] 已落库待复核 | message_id=%s", email.message_id
            )
    except Exception:
        logger.exception(
            "[human_review] 落库失败（不影响轮询） | message_id=%s", email.message_id
        )
