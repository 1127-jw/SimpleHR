"""意图识别「人工复核」待办的仓储。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, Sequence

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import db_now
from app.models.mail_review import MailReviewModel, MailReviewStatusEnum
from utils.emailbot.intent import MailIntentResult
from utils.emailbot.models import ParsedEmail

from . import BaseRepo


class MailReviewRepo(BaseRepo):
    """待复核邮件的读写。"""

    async def create_from_email(
        self, email: ParsedEmail, result: MailIntentResult
    ) -> tuple[MailReviewModel, bool]:
        """落一条待复核记录（幂等）。

        Returns:
            ``(记录, 是否新建)``。``message_id`` 已存在时直接返回旧记录。
        """
        existing = await self.session.scalar(
            select(MailReviewModel).where(MailReviewModel.message_id == email.message_id)
        )
        if existing is not None:
            return existing, False

        row = MailReviewModel(
            email_uid=email.uid,
            message_id=email.message_id,
            from_address=email.from_.address,
            from_name=email.from_.name,
            subject=email.subject,
            # 留全量：复核要靠上下文判断意图
            body_excerpt=email.text,
            attachment_paths=[str(a.saved_path) for a in email.attachments if a.saved_path],
            intent_guess=result.intent.value,
            sub_intent=result.sub_intent,
            confidence=result.confidence,
            reason=result.reason,
            evidence=result.evidence,
            status=MailReviewStatusEnum.PENDING,
        )
        self.session.add(row)
        await self.session.flush()
        return row, True

    async def get_by_id(self, review_id: str) -> Optional[MailReviewModel]:
        return await self.session.get(MailReviewModel, review_id)

    async def list_reviews(
        self,
        *,
        status: Optional[str] = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[MailReviewModel], int]:
        """分页列表，返回 ``(记录, 总数)``。"""
        where = (
            MailReviewModel.status == status if status else None
        )
        stmt = select(MailReviewModel)
        count_stmt = select(MailReviewModel)
        if where is not None:
            stmt = stmt.where(where)
            count_stmt = count_stmt.where(where)
        from sqlalchemy import func

        total = (await self.session.scalar(
            select(func.count()).select_from(count_stmt.subquery())
        )) or 0
        rows = list(await self.session.scalars(
            stmt.order_by(MailReviewModel.created_at.desc())
            .offset((page - 1) * size)
            .limit(size)
        ))
        return rows, total

    async def resolve(
        self,
        review_id: str,
        *,
        resolver_id: str,
        status: str,
        resolution: str,
        resolved_at: Optional[datetime] = None,
    ) -> MailReviewModel:
        """标记处理结果。

        Raises:
            ValueError: 记录不存在或已被处理。
        """
        row = await self.get_by_id(review_id)
        if row is None:
            raise ValueError("复核记录不存在")
        if row.status != MailReviewStatusEnum.PENDING:
            raise ValueError(f"当前状态 {row.status} 不可重复处理")
        row.status = status
        row.resolver_id = resolver_id
        row.resolution = resolution
        row.resolved_at = resolved_at or db_now()
        await self.session.flush()
        return row

    # ==================== 并发抢占 ====================

    async def claim(
        self,
        review_id: str,
        *,
        resolver_id: str,
        lease_seconds: int = 600,
    ) -> bool:
        """原子抢占一条待复核记录，避免两个 HR 同时点「执行」重复跑动作。

        不改 ``status``（仍为 PENDING），而是写 ``resolver_id`` + ``claimed_at``
        作为租约：条件更新能命中才说明抢到了。

        * 释放：``release_claim``（动作失败时调用，允许他人立刻重试）
        * 超时：租约过期后自动可被重新抢占，避免进程崩溃导致记录永久锁死

        注意：租约用 ``claimed_at``（认领时间）而**不是** ``resolved_at``
        （处理完成时间）。后者面向展示与统计，不能被租约语义污染。

        Returns:
            True 表示抢占成功；False 表示记录已不存在 / 已被处理 / 正被他人处理。
        """
        now = db_now()
        result = await self.session.execute(
            update(MailReviewModel)
            .where(
                MailReviewModel.id == review_id,
                MailReviewModel.status == MailReviewStatusEnum.PENDING,
                or_(
                    MailReviewModel.claimed_at.is_(None),
                    MailReviewModel.claimed_at < now - timedelta(seconds=lease_seconds),
                ),
            )
            .values(resolver_id=resolver_id, claimed_at=now)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount > 0

    async def release_claim(self, review_id: str, *, resolver_id: str) -> None:
        """释放租约（动作执行失败时调用），让记录回到「无人认领」状态。"""
        await self.session.execute(
            update(MailReviewModel)
            .where(
                MailReviewModel.id == review_id,
                MailReviewModel.status == MailReviewStatusEnum.PENDING,
                MailReviewModel.resolver_id == resolver_id,
            )
            .values(resolver_id=None, claimed_at=None)
            .execution_options(synchronize_session=False)
        )
