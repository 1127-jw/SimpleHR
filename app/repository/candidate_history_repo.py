"""候选人状态变更历史（状态链）仓储。

链的写入是「先占位、后插入」两步，必须在同一事务内：

① ``UPDATE ... SET next_id = :new_id WHERE candidate_id = :cid AND next_id IS NULL``
   —— 抢「链上最新」位置；影响 0 行说明链断/并发冲突，必须回滚。
② ``INSERT`` 新记录（``next_id = NULL``）。

归档 / 结算 = 追加最后一条，并把**该条**的 ``next_id`` 置为 ``'-1'`` / ``'-2'``。

设计说明见 ``docs/candidate_status_history_design.md``。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import CandidateModel, CandidateStatusEnum
from app.models.candidate_status_history import (
    NEXT_ID_ARCHIVED,
    NEXT_ID_SETTLED,
    CandidateStatusHistoryModel,
)
from . import BaseRepo


class ChainBrokenError(RuntimeError):
    """链上「最新」位置被抢占——并发写入或历史数据断链。

    宁可报错回滚，也不要插出一条 ``next_id IS NULL`` 的重复记录：
    那会让「谁是最新的」变得不唯一，超时巡检的判据直接失效。
    """


class CandidateStatusHistoryRepo(BaseRepo):
    """状态变更链的读写。"""

    # ==================== 写入 ====================

    async def record_change(
        self,
        candidate_id: str,
        *,
        to_status: str,
        from_status: Optional[str] = None,
        candidate_email: Optional[str] = None,
        position_id: Optional[str] = None,
        position_title: Optional[str] = None,
        changed_at: Optional[datetime] = None,
        next_id: Optional[str] = None,
    ) -> CandidateStatusHistoryModel:
        """追加一条状态变更记录，并把上一条的 ``next_id`` 指向它。

        Args:
            next_id: 追加完这条之后，**这条**的 next_id 应该是多少。
                传 ``NEXT_ID_ARCHIVED`` / ``NEXT_ID_SETTLED`` 表示该条即链尾
                （归档 / 结算）；默认 ``None`` 表示链上最新（流程仍在进行）。

        Raises:
            ChainBrokenError: 抢不到「最新」位置（并发写入 / 断链）。
        """
        ts = changed_at or self._now()
        new_id = self._new_id()

        claimed = await self.session.execute(
            update(CandidateStatusHistoryModel)
            .where(
                CandidateStatusHistoryModel.candidate_id == candidate_id,
                CandidateStatusHistoryModel.next_id.is_(None),
            )
            .values(next_id=new_id)
        )
        if claimed.rowcount == 0:
            # 该候选人此前没有任何链记录时，允许作为「链头」直接插入
            has_any = await self.session.scalar(
                select(CandidateStatusHistoryModel.id)
                .where(CandidateStatusHistoryModel.candidate_id == candidate_id)
                .limit(1)
            )
            if has_any:
                raise ChainBrokenError(
                    f"候选人 {candidate_id} 的状态链抢位失败："
                    "已有记录但没有 next_id IS NULL 的链尾（并发写入或断链）"
                )

        row = CandidateStatusHistoryModel(
            id=new_id,
            candidate_id=candidate_id,
            candidate_email=candidate_email,
            position_id=position_id,
            position_title=position_title,
            from_status=from_status,
            to_status=to_status,
            changed_at=ts,
            next_id=next_id,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def start_new_chain(
        self,
        candidate_id: str,
        *,
        to_status: str,
        from_status: Optional[str] = None,
        candidate_email: Optional[str] = None,
        position_id: Optional[str] = None,
        position_title: Optional[str] = None,
    ) -> CandidateStatusHistoryModel:
        """开启一条**新链**：把旧的活跃链尾置为归档，再插入链头。

        用于「归档 / 结算 / 失败后重新发起」——同一个 ``candidate_id`` 可以有多条链
        （旧链保留完整历史），但任一时刻**最多一条活跃**（``next_id IS NULL``）。

        与 ``record_change`` 的区别：后者要求已存在活跃链来抢位，本方法负责
        「终止旧的 + 开新的」这个组合动作，失败重试走这里。
        """
        await self.session.execute(
            update(CandidateStatusHistoryModel)
            .where(
                CandidateStatusHistoryModel.candidate_id == candidate_id,
                CandidateStatusHistoryModel.next_id.is_(None),
            )
            .values(next_id=NEXT_ID_ARCHIVED)
        )
        row = CandidateStatusHistoryModel(
            id=self._new_id(),
            candidate_id=candidate_id,
            candidate_email=candidate_email,
            position_id=position_id,
            position_title=position_title,
            from_status=from_status,
            to_status=to_status,
            changed_at=self._now(),
            next_id=None,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def mark_archived(
        self,
        candidate_id: str,
        *,
        to_status: str,
        from_status: Optional[str] = None,
        **kwargs,
    ) -> CandidateStatusHistoryModel:
        """归档收尾：追加链尾记录并置 ``next_id = '-1'``。"""
        return await self.record_change(
            candidate_id,
            to_status=to_status,
            from_status=from_status,
            next_id=NEXT_ID_ARCHIVED,
            **kwargs,
        )

    async def mark_settled(
        self,
        candidate_id: str,
        *,
        to_status: str = CandidateStatusEnum.WAITING_FOR_INTERVIEW.value,
        from_status: Optional[str] = None,
        **kwargs,
    ) -> CandidateStatusHistoryModel:
        """结算收尾：追加链尾记录并置 ``next_id = '-2'``（流程正常完成）。"""
        return await self.record_change(
            candidate_id,
            to_status=to_status,
            from_status=from_status,
            next_id=NEXT_ID_SETTLED,
            **kwargs,
        )

    async def touch_last_email(
        self, candidate_id: str, *, received_at: datetime
    ) -> bool:
        """更新该候选人**当前活跃记录**的「最后接到邮件时间」。

        Returns:
            是否真的更新了一行（没有活跃记录时返回 False，调用方只需记日志）。
        """
        result = await self.session.execute(
            update(CandidateStatusHistoryModel)
            .where(
                CandidateStatusHistoryModel.candidate_id == candidate_id,
                CandidateStatusHistoryModel.next_id.is_(None),
            )
            .values(last_email_received_at=received_at)
        )
        return result.rowcount > 0

    # ==================== 读取 ====================

    async def get_active(
        self, candidate_id: str
    ) -> Optional[CandidateStatusHistoryModel]:
        """链上最新（活跃）记录；没有则 None。"""
        return await self.session.scalar(
            select(CandidateStatusHistoryModel)
            .where(
                CandidateStatusHistoryModel.candidate_id == candidate_id,
                CandidateStatusHistoryModel.next_id.is_(None),
            )
            .limit(1)
        )

    async def get_active_by_email(
        self, email: str
    ) -> Optional[CandidateStatusHistoryModel]:
        """按邮箱找「链上最新」记录——建图查重用。"""
        return await self.session.scalar(
            select(CandidateStatusHistoryModel)
            .where(
                CandidateStatusHistoryModel.candidate_email == email,
                CandidateStatusHistoryModel.next_id.is_(None),
            )
            .limit(1)
        )

    async def list_stale_negotiating(
        self, cutoff: datetime, limit: int = 200
    ) -> Sequence[CandidateStatusHistoryModel]:
        """超时巡检的取数：协商中 + 链上最新 + 起算时间早于 cutoff。

        起算时间取 ``COALESCE(last_email_received_at, changed_at)``：
        「邀请刚发出、候选人还没回过信」时后者兜底，否则 NULL 行永远漏判。
        """
        return list(await self.session.scalars(
            select(CandidateStatusHistoryModel)
            .where(
                CandidateStatusHistoryModel.to_status
                == CandidateStatusEnum.NEGOTIATING.value,
                CandidateStatusHistoryModel.next_id.is_(None),
                func.coalesce(
                    CandidateStatusHistoryModel.last_email_received_at,
                    CandidateStatusHistoryModel.changed_at,
                ) < cutoff,
            )
            .order_by(CandidateStatusHistoryModel.changed_at)
            .limit(limit)
        ))

    # ==================== 内部 ====================

    def _now(self) -> datetime:
        from app.core.time import db_now

        return db_now()

    def _new_id(self) -> str:
        from shortuuid import uuid

        return f"csh_{uuid()}"
