"""意图识别「人工复核」的执行逻辑。

复核人确认意图后，**按该意图执行对应动作**（恢复招聘图 / 建投递记录 / 标记忽略），
而不是只改个状态——否则人工判定没有实际作用。

动作实现复用 ``utils.emailbot.notify`` 里已有的私有函数，避免两套逻辑漂移。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import AsyncSessionFactory
from app.core.time import db_now
from app.models.mail_review import MailReviewModel, MailReviewStatusEnum
from app.repository.mail_review_repo import MailReviewRepo
from utils.emailbot.intent import MailIntent
from utils.emailbot.models import Attachment, EmailAddress, ParsedEmail
from utils.emailbot.notify import (
    _find_candidate_by_email,
    _handle_resume_submission,
    _reply_progress,
    _resume_candidate_graph,
)

settings = get_settings()
logger = logging.getLogger(__name__)

# 「人工确认后可直接执行」的意图
_EXECUTABLE_INTENTS = {
    MailIntent.INTERVIEW_REPLY,
    MailIntent.RESUME_SUBMISSION,
    MailIntent.PROGRESS_INQUIRY,
}


def _rebuild_email(row: MailReviewModel) -> ParsedEmail:
    """从落库字段重建 ``ParsedEmail``，供复用既有的动作函数。

    ``raw`` 拿不到（没存原始 MIME），但 ``_resume_candidate_graph`` /
    ``_handle_resume_submission`` / ``_reply_progress`` 只用到
    ``from_`` / ``subject`` / ``text`` / ``attachments``。
    """
    return ParsedEmail(
        uid=row.email_uid,
        message_id=row.message_id,
        subject=row.subject,
        from_=EmailAddress(name=row.from_name, address=row.from_address),
        text=row.body_excerpt,
        attachments=[
            Attachment(filename=Path(p).name, saved_path=Path(p), content_type="", size=0)
            for p in (row.attachment_paths or [])
        ],
    )


async def enqueue_review(
    session: AsyncSession, email: ParsedEmail, result
) -> tuple[MailReviewModel, bool]:
    """把待复核邮件落库（幂等）。"""
    return await MailReviewRepo(session).create_from_email(email, result)


async def list_reviews(
    session: AsyncSession, *, status: Optional[str], page: int, size: int
) -> tuple[list[MailReviewModel], int]:
    return await MailReviewRepo(session).list_reviews(
        status=status, page=page, size=size
    )


async def execute_intent(email: ParsedEmail, intent: MailIntent) -> None:
    """按人工选定的意图执行动作。"""
    if intent not in _EXECUTABLE_INTENTS:
        logger.info("[mail_review] %s 无自动动作，仅记录 | from=%s", intent, email.sender)
        return

    if intent is MailIntent.INTERVIEW_REPLY:
        candidate, position, interviewer = await _find_candidate_by_email(
            email.from_.address
        )
        await _resume_candidate_graph(email, candidate, position, interviewer)
    elif intent is MailIntent.RESUME_SUBMISSION:
        await _handle_resume_submission(email)
    elif intent is MailIntent.PROGRESS_INQUIRY:
        candidate, _, _ = await _find_candidate_by_email(email.from_.address)
        await _reply_progress(email, candidate)


async def resolve_review(
    session: AsyncSession,
    review_id: str,
    *,
    resolver_id: str,
    intent: Optional[str],
    action: str,
) -> MailReviewModel:
    """处理一条复核记录。

    Args:
        action: ``"execute"`` 按 ``intent`` 执行动作；``"ignore"`` 仅标记忽略。
        intent: ``action == "execute"`` 时必填，取值见 ``MailIntent``。

    Raises:
        ValueError: 记录不存在 / 已处理 / 参数不合法。
    """
    repo = MailReviewRepo(session)
    async with session.begin():
        row = await repo.get_by_id(review_id)
        if row is None:
            raise ValueError("复核记录不存在")
        if row.status != MailReviewStatusEnum.PENDING:
            raise ValueError(f"当前状态 {row.status} 不可重复处理")

        if action == "ignore":
            # 忽略没有副作用且天然幂等（并发两次结果相同），
            # 因此**不需要**租约，直接落状态即可。
            return await repo.resolve(
                review_id,
                resolver_id=resolver_id,
                status=MailReviewStatusEnum.IGNORED,
                resolution="人工忽略",
                resolved_at=db_now(),
            )

        if action != "execute":
            raise ValueError(f"未知的处理动作: {action!r}")
        if not intent:
            raise ValueError("执行动作必须指定意图")
        try:
            parsed_intent = MailIntent(intent)
        except ValueError as e:
            raise ValueError(f"未知的意图: {intent!r}") from e
        email = _rebuild_email(row)

        # 原子抢占：动作可能有副作用（恢复状态图、发邮件），
        # 两个 HR 同时点「执行」会重复跑。条件更新命中才算抢到。
        # 租约由 Settings 给出，且启动期已校验「租约 > 动作硬超时」。
        if not await repo.claim(
            review_id,
            resolver_id=resolver_id,
            lease_seconds=settings.MAIL_REVIEW_CLAIM_LEASE_SECONDS,
        ):
            raise ValueError("该记录正在被其他人处理，请刷新后重试")

    # 先执行动作，再落处理结果：动作失败时保持 PENDING，
    # 避免「标记已处理但其实没做」。
    #
    # 硬超时兜底：动作可能触发状态图恢复（LLM + 钉钉 + 发信），
    # 没有上界的话一次模型挂起就会让记录长期处于「已认领」。
    # 超时被取消后 checkpoint 仍停在原中断点，重试是安全的。
    try:
        await asyncio.wait_for(
            execute_intent(email, parsed_intent),
            timeout=settings.MAIL_REVIEW_EXECUTE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        # 动作失败 / 超时：释放租约，让别人（或本人重试）能立刻再处理，
        # 而不是等租约自然过期。
        # 注意：只兜 Exception；外层的 CancelledError（关停）不在此列，
        # 那种情况让租约自然过期更安全，避免在已取消的任务里做 DB IO。
        try:
            async with session.begin():
                await MailReviewRepo(session).release_claim(
                    review_id, resolver_id=resolver_id
                )
        except Exception:  # noqa: BLE001 - 释放失败只影响可重试延迟
            logger.warning(
                "[mail_review] 释放租约失败 | review_id=%s", review_id, exc_info=True
            )

        if isinstance(exc, TimeoutError):
            logger.warning(
                "[mail_review] 动作执行超时（%ss），已释放认领 | review_id=%s",
                settings.MAIL_REVIEW_EXECUTE_TIMEOUT_SECONDS, review_id,
            )
            raise TimeoutError(
                "复核动作执行超时（"
                f"{settings.MAIL_REVIEW_EXECUTE_TIMEOUT_SECONDS:.0f}s），"
                "记录已释放，请稍后重试"
            ) from exc
        raise

    async with session.begin():
        return await MailReviewRepo(session).resolve(
            review_id,
            resolver_id=resolver_id,
            status=MailReviewStatusEnum.RESOLVED,
            resolution=f"人工确认意图：{parsed_intent.value}",
            resolved_at=db_now(),
        )
