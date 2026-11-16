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
from app.models.candidate import CandidateStatusEnum
from app.repository.resume_repo import CandidateRepo
from app.schemas.candidate_schema import CandidateSchema
from app.schemas.position_schema import PositionSchema
from app.schemas.user_schema import UserSH
from app.workflow.candidate_graph import build_candidate_graph

logger = logging.getLogger(__name__)

settings = get_settings()

# 允许恢复协商图的候选人状态（与既有兜底逻辑保持一致）
_RESUMABLE_STATUS = (
    CandidateStatusEnum.MANUAL_PASSED,
    CandidateStatusEnum.AI_FILTER_PASSED,
)


async def _find_candidate_by_email(email_address: str):
    """根据邮箱地址查找候选人（含职位与面试官）。"""
    async with AsyncSessionFactory() as session:
        repo = CandidateRepo(session)
        candidate = await repo.get_by_email(email_address)
        if not candidate:
            return None, None, None
        # 必须在 session 内访问关联对象并转 Schema，避免 DetachedInstanceError
        return (
            CandidateSchema.model_validate(candidate),
            PositionSchema.model_validate(candidate.position),
            UserSH.model_validate(candidate.creator),
        )


# 模块级缓存：复用同一个编译图实例读取状态快照，避免每次邮件轮询重建
_graph = None


def _get_graph():
    global _graph
    if _graph is None:
        _graph = build_candidate_graph(get_checkpointer())
    return _graph


async def _waiting_reply(thread_id: str) -> bool:
    """该 thread 的图是否停在中断点（等待候选人回复）。

    用 ``StateSnapshot`` 判断：停在中断点时 ``next`` 非空（待执行节点），
    图运行结束（到达 END）时 ``next`` 为空。
    """
    try:
        graph = _get_graph()
        snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    except Exception:
        logger.exception("读取 checkpoint 失败 | thread_id=%s", thread_id)
        return False
    if not snapshot:
        return False
    # next 非空 = 还有待执行节点 = 停在中断点
    return bool(snapshot.next)


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
        # thread_id 与建图时保持一致（app/tasks/agent_tasks.py 用 candidate.email）
        thread_id = candidate_schema.email
    else:
        role = SenderRole.UNKNOWN
        candidate_status = ""
        thread_id = email.from_.address

    # ---- L3 前置校验：是否在等待回复 ----
    waiting_reply = await _waiting_reply(thread_id) if candidate_schema else False

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
        await _resume_candidate_graph(
            email, candidate_schema, position_schema, interviewer_schema
        )
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
) -> None:
    """恢复招聘图：仅在候选人在协商中、且 checkpoint 停在等待回复中断点。"""
    if not candidate_schema:
        logger.warning("未找到候选人，不恢复图 | from=%s", email.from_.address)
        return

    if candidate_schema.status not in _RESUMABLE_STATUS:
        logger.info(
            "候选人不在协商中，不恢复图 | candidate_id=%s | status=%s",
            candidate_schema.id, candidate_schema.status,
        )
        return

    thread_id = candidate_schema.email
    if not await _waiting_reply(thread_id):
        logger.info("无有效等待回复 checkpoint，不恢复图 | thread_id=%s", thread_id)
        return

    if not settings.GRAPH_OPEN:
        logger.info("GRAPH_OPEN=false，仅记录不执行 | thread_id=%s", thread_id)
        return

    candidate_graph = build_candidate_graph(get_checkpointer())
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
    """低置信 / 歧义：不 resume、不自动回，记日志待人工复核。
    TODO: 接入实际通知（钉钉工作通知 / 邮件），可复用
    """

    logger.warning(
        "[human_review] uid=%s | intent=%s | conf=%.2f | reason=%s",
        email.uid, result.intent.value, result.confidence, result.reason,
    )
